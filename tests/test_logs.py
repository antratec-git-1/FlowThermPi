"""Tests for the protocol: heizapp.logs (the file, without secrets), heizapp.support („Protokoll speichern“ and its API)
and the root part install.py --logs that collects the Pi's system logs.

Nothing on this computer is read or run: FakePi answers in place of journalctl, systemctl, ip and vcgencmd, and its
answers hold the very secrets that must never leave the Pi.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import install  # noqa: E402
from fake_heater import FakeHeater  # noqa: E402
from heizapp import logs, support  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import Service  # noqa: E402
from heizapp.support import Support, SystemLog  # noqa: E402
from test_install import Base as InstallBase  # noqa: E402

TOKEN = "Zq8RkW3nPv7LxT2mYb5HcJ9d"          # a heater's token (24 characters, as in the QR code)
WLAN = "Sommer-Regen-2026"                   # the WLAN password in NetworkManager's file
OTHER_WLAN = "AnderesPasswort99"             # one the Pi does not keep: only its look gives it away
APP_MQTT, Z2M_MQTT = "a1b2c3d4e5f6a7b8c9d0", "f0e9d8c7b6a5f4e3d2c1"
APP_PASSWORD = "Haustuer-gruen-7"            # HEIZAPP_PASSWORD
LOGIN = "1a2b3c4d5e6f7a"                     # the code in Tailscale's login link
SECRETS = (TOKEN, WLAN, OTHER_WLAN, APP_MQTT, Z2M_MQTT, APP_PASSWORD, LOGIN)


def until(check, seconds: float = 5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("timed out")


class LogBase(unittest.TestCase):
    """logs.setup() changes the logging of the whole process – put back afterwards for the other tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root, app = logging.getLogger(), logging.getLogger("heizapp")
        saved = (list(root.handlers), root.level, app.level, threading.excepthook, sys.excepthook,
                 logs._folder, logs._file, set(logs._secrets), logs._known, list(logs._recent))

        def restore():
            for handler in list(root.handlers):
                root.removeHandler(handler)
                if getattr(handler, "_flowthermpi", False):
                    handler.close()
            for handler in saved[0]:
                root.addHandler(handler)
            root.setLevel(saved[1])
            app.setLevel(saved[2])
            threading.excepthook, sys.excepthook = saved[3], saved[4]
            logs._folder, logs._file = saved[5], saved[6]
            with logs._lock:
                logs._secrets.clear()
                logs._secrets.update(saved[7])
                logs._known = saved[8]
            logs._recent.clear()
            logs._recent.extend(saved[9])

        self.addCleanup(restore)
        logs._recent.clear()
        self.folder = self.tmp / "logs"
        self.file = logs.setup(self.folder, console=False)


# ---------------------------------------------------------------------- heizapp.logs
class LogFileTest(LogBase):
    def test_secrets_never_reach_the_file(self):
        logs.keep_secret(TOKEN)
        logs.keep_secret("1234")    # too short: it would blank out ordinary numbers
        basic = base64.b64encode(f"heizung:{APP_PASSWORD}".encode()).decode()
        log = logging.getLogger("heizapp.test")
        log.warning("Antwort von coap://192.168.1.10/Json?token=%s: 4.01", "ganzanderes123")
        log.warning("Token %s abgelehnt", TOKEN)
        log.warning('WLAN-Anfrage {"ssid": "Zuhause", "password": "mein \\"geheimes\\" Passwort"}')
        log.warning('Zigbee: {"mqtt_password": "%s", "pin_required": true}', Z2M_MQTT)
        log.warning("Umgebung: HEIZAPP_PASSWORD=%s ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD=%s", APP_PASSWORD, APP_MQTT)
        log.warning("Authorization: Basic %s", basic)
        log.warning("Bitte anmelden: https://login.tailscale.com/a/%s – Code 1234", LOGIN)
        log.warning("QR-Code: data:image/png;base64,%s", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB" * 4)
        text = logs.read_all()
        for secret in (TOKEN, "ganzanderes123", "geheimes", basic, LOGIN, "iVBORw0KGgo", Z2M_MQTT,
                       APP_PASSWORD, APP_MQTT):
            self.assertNotIn(secret, text)
        self.assertIn("Json?token=••• 4.01", text)   # rather a character too many than one too few
        self.assertIn("Token ••• abgelehnt", text)
        self.assertIn('{"ssid": "Zuhause", "password": "•••"}', text)
        self.assertIn('{"mqtt_password": "•••", "pin_required": true}', text)
        self.assertIn("HEIZAPP_PASSWORD=••• ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD=•••", text)
        self.assertEqual(logs.redact("spin=3 Setpoint=21 pinned"), "spin=3 Setpoint=21 pinned")   # words stay
        self.assertIn("Authorization: Basic •••", text)
        self.assertIn("https://login.tailscale.com/a/••• – Code 1234", text)
        self.assertEqual(logs.redact(f"x {TOKEN} y"), "x ••• y")

    def test_each_line_says_when_how_bad_and_where(self):
        logging.getLogger("heizapp.service").warning("Heizung Bad antwortet nicht")
        logging.getLogger("heizapp").info("FlowThermPi startet")
        lines = logs.read_all().splitlines()
        stamp = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} [+-]\d{4} "
        self.assertRegex(lines[-2], stamp + r"WARNING service  Heizung Bad antwortet nicht$")
        self.assertRegex(lines[-1], stamp + r"INFO {4}app {6}FlowThermPi startet$")
        self.assertEqual(self.file, self.folder / "flowthermpi.log")
        self.assertEqual(oct(self.folder.stat().st_mode & 0o777), "0o700")

    def test_detailed_entries_only_in_the_file_and_only_when_asked(self):
        console = io.StringIO()
        with redirect_stderr(console):
            logs.setup(self.folder)
        log = logging.getLogger("heizapp.coap")
        log.debug("Versuch 2 von 3")
        logs.set_detail(True)
        self.assertTrue(logs.detail())
        log.debug("Versuch 3 von 3")
        log.info("Heizung erreichbar")
        logs.set_detail(False)
        log.debug("Versuch 4 von 3")
        text = logs.read_all()
        self.assertNotIn("Versuch 2", text)
        self.assertIn("DEBUG   coap     Versuch 3 von 3", text)
        self.assertNotIn("Versuch 4", text)
        self.assertRegex(console.getvalue(), r"^\d\d:\d\d:\d\d INFO coap: Heizung erreichbar\n$")   # the journal: short

    def test_old_entries_move_to_older_files_and_are_read_oldest_first(self):
        with mock.patch.object(logs, "MAX_BYTES", 4000), mock.patch.object(logs, "BACKUPS", 3):
            logs.setup(self.folder, console=False)
            log = logging.getLogger("heizapp.service")
            for i in range(400):
                log.info("Eintrag %04d", i)
            self.assertEqual([p.name for p in logs.files()],
                             ["flowthermpi.log.3", "flowthermpi.log.2", "flowthermpi.log.1", "flowthermpi.log"])
            numbers = [int(line.rsplit(" ", 1)[1]) for line in logs.read_all().splitlines()]
            self.assertEqual(numbers, list(range(numbers[0], 400)))   # in order, nothing missing, newest last
            self.assertGreater(numbers[0], 0)                          # the oldest are gone: the SD card stays free
            self.assertEqual(logs.size(), sum(p.stat().st_size for p in logs.files()))
            tail = logs.read_all(limit=1000)
            self.assertLessEqual(len(tail.encode()), 1000)
            self.assertTrue(tail.endswith("Eintrag 0399\n"))
            self.assertRegex(tail, r"^\d{4}-\d\d-\d\d ")                # from the start of a line
            self.assertRegex(logs.oldest(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")   # for the page (localTime)

    def test_an_unclean_end_is_noticed_at_the_next_start(self):
        self.assertIsNone(logs.mark_running())
        logging.getLogger("heizapp").info("läuft")
        logs.setup(self.folder, console=False)      # power cut: the next start finds the marker
        self.assertRegex(logs.mark_running(), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        logs.mark_stopped()
        logs.setup(self.folder, console=False)
        self.assertIsNone(logs.mark_running())

    def test_errors_in_the_background_are_written_down(self):
        def broken():
            raise ValueError("kaputt")

        thread = threading.Thread(target=broken, name="abfrage")
        thread.start()
        thread.join()
        text = logs.read_all()
        self.assertIn("ERROR   app      Unerwarteter Fehler im Hintergrund (abfrage)", text)
        self.assertIn("ValueError: kaputt", text)
        self.assertIn("Unerwarteter Fehler im Hintergrund (abfrage)", logs.recent()[-1])

    def test_without_a_writable_folder_the_app_runs_anyway(self):
        blocker = self.tmp / "datei"
        blocker.write_text("")
        self.assertIsNone(logs.setup(blocker / "logs", console=False))
        self.assertEqual((logs.files(), logs.read_all(), logs.size(), logs.oldest()), ([], "", 0, None))
        self.assertIn("lässt sich nicht schreiben", logs.recent()[-1])
        self.assertIsNone(logs.mark_running())


# ---------------------------------------------------------------------- „Protokoll speichern“ and its API
class SupportApiTest(LogBase):
    def setUp(self):
        super().setUp()
        self.heater = FakeHeater(token=TOKEN, idu="91110001")
        self.addCleanup(self.heater.close)
        conf = self.tmp / "heizungen.conf"
        conf.write_text(f"127.0.0.1:{self.heater.port} {TOKEN}\n")
        self.service = Service(str(conf), str(self.tmp / "data.json"), poll_interval=3600)
        for e in self.service.entries:
            e.device.timeout, e.device.retries = 0.3, 0
        self.service.poll_all(full=True)
        self.support = Support(self.service, poll=0.01)
        self.server = make_server(self.service, "127.0.0.1", 0, support=self.support)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = None

    def raw(self, method, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = f"heizung_einrichtung={self.cookie}"
        data = json.dumps(body if body is not None else {}).encode() if method != "GET" else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                status, raw, answer = res.status, res.read(), res.headers
        except urllib.error.HTTPError as err:
            with err:
                status, raw, answer = err.code, err.read(), err.headers
        cookie = answer.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        return status, answer, raw

    def call(self, method, path, body=None, expect=200):
        status, _, raw = self.raw(method, path, body)
        payload = json.loads(raw or b"{}")
        self.assertEqual(status, expect, payload)
        return payload

    def download(self, made) -> dict[str, str]:
        status, answer, raw = self.raw("GET", f"/api/logs/{made['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(answer["Content-Type"], "application/zip")
        self.assertEqual(answer["Content-Disposition"], f'attachment; filename="{made["name"]}"')
        self.assertEqual(len(raw), made["size"])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(archive.namelist(), ["LIESMICH.txt", "uebersicht.txt", "app.log", "system.txt", "daten.json"])
            return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}

    def test_residents_save_it_without_the_pin_and_nothing_secret_is_in_it(self):
        self.call("PUT", "/api/setup/pin", {"pin": "4711"})
        self.cookie = None                                    # the residents have no PIN
        logging.getLogger("heizapp.device").warning("Antwort auf Json?token=%s war 4.01 – Token %s", TOKEN, TOKEN)
        made = self.call("POST", "/api/logs/prepare")
        self.assertRegex(made["name"], r"^flowthermpi-protokoll-[A-Za-z0-9-]+-\d{8}-\d{4}\.zip$")
        self.assertEqual((made["system"], made["mail"]), (False, None))
        self.assertIn("nicht mit dem Installationsbefehl", made["missing"])
        files = self.download(made)
        for name, text in files.items():
            self.assertNotIn(TOKEN, text, name)
            self.assertNotIn('"hash"', text, name)            # nor the PIN's hash
        self.assertIn("Antwort auf Json?token=••• war 4.01 – Token •••", files["app.log"])
        overview = files["uebersicht.txt"]
        self.assertIn("Heizungen (1, davon 1 erreichbar)", overview)
        self.assertIn("Token: ja", overview)
        self.assertIn("PIN der Einrichtung:  gesetzt", overview)
        self.assertIn("WARNING device   Antwort auf Json?token=••• war 4.01", overview.split("Letzte Warnungen")[1])
        self.assertEqual(json.loads(files["daten.json"])["settings"]["pin"], "•••")
        self.assertIn("nicht mit dem Installationsbefehl", files["system.txt"])
        self.assertIn("fehlen diesmal", files["LIESMICH.txt"])
        until(lambda: "POST /api/logs/prepare → 200" in logs.read_all())   # every press is written down

    def test_the_settings_need_the_pin(self):
        self.call("PUT", "/api/setup/pin", {"pin": "4711"})   # this browser stays unlocked
        unlocked, self.cookie = self.cookie, None
        self.assertTrue(self.call("PUT", "/api/logs/settings", {"email": "helfer@beispiel.de"}, expect=403)["pin_required"])
        self.assertTrue(self.call("PUT", "/api/logs/settings", {"detail": True}, expect=403)["pin_required"])
        self.assertFalse(logs.detail())
        self.cookie = unlocked
        error = self.call("PUT", "/api/logs/settings", {"email": "helfer at beispiel"}, expect=400)["error"]
        self.assertIn("nicht wie eine E-Mail-Adresse", error)
        res = self.call("PUT", "/api/logs/settings", {"email": " helfer@beispiel.de "})
        self.assertEqual((res["message"], res["logs"]["email"]), ("E-Mail-Adresse gespeichert.", "helfer@beispiel.de"))
        self.cookie = None
        self.assertEqual(self.call("GET", "/api/state")["logs"]["email"], "helfer@beispiel.de")   # the residents' view
        made = self.call("POST", "/api/logs/prepare")
        self.assertEqual(made["mail"]["to"], "helfer@beispiel.de")
        self.assertRegex(made["mail"]["subject"], r"^FlowThermPi-Protokoll \S+ vom \d\d\.\d\d\.\d{4} \d\d:\d\d$")
        self.assertIn(made["name"], made["mail"]["body"])
        self.cookie = unlocked
        self.assertIsNone(self.call("PUT", "/api/logs/settings", {"email": ""})["logs"]["email"])

    def test_the_detailed_log_ends_by_itself(self):
        self.support.start()
        self.addCleanup(self.support.stop)
        res = self.call("PUT", "/api/logs/settings", {"detail": True})
        self.assertTrue(logs.detail())
        self.assertTrue(res["message"].startswith("Ausführliches Protokoll an – bis "))
        end = datetime.fromisoformat(res["logs"]["detail_until"])
        self.assertAlmostEqual((end - datetime.now().astimezone()).total_seconds(), 24 * 3600, delta=60)
        self.call("GET", "/api/state?nicht-ins-protokoll=1")      # now every call of the page is written down …
        until(lambda: "GET /api/state → 200" in logs.read_all())
        self.assertNotIn("nicht-ins-protokoll", logs.read_all())  # … without what follows the „?“
        with self.service.store.edit() as d:                     # a day later
            d["settings"]["log_detail_until"] = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()
        self.support._wake.set()
        until(lambda: not logs.detail())
        until(lambda: "Ausführliches Protokoll beendet" in logs.read_all())
        self.assertNotIn("log_detail_until", self.service.store.data["settings"])
        self.assertIsNone(self.call("GET", "/api/state")["logs"]["detail_until"])

    def test_a_restart_keeps_the_detailed_log_until_its_end(self):
        later = datetime.now().astimezone() + timedelta(hours=3)
        with self.service.store.edit() as d:
            d.setdefault("settings", {})["log_detail_until"] = later.isoformat(timespec="seconds")
        first = Support(self.service)
        first.start()
        self.addCleanup(first.stop)
        self.assertTrue(logs.detail())
        first.stop()
        logs.set_detail(False)
        with self.service.store.edit() as d:
            d["settings"]["log_detail_until"] = "2026-01-01T00:00:00+01:00"
        again = Support(self.service)
        again.start()
        self.addCleanup(again.stop)
        self.assertFalse(logs.detail())
        self.assertNotIn("log_detail_until", self.service.store.data["settings"])

    def test_prepared_files_are_kept_only_for_a_while(self):
        self.assertIn("nicht mehr da", self.call("GET", "/api/logs/0123456789abcdef", expect=404)["error"])
        made = [self.call("POST", "/api/logs/prepare") for _ in range(4)]
        self.call("GET", f"/api/logs/{made[0]['id']}", expect=404)   # at most three at a time
        self.download(made[3])
        with mock.patch.object(support, "KEEP", 0):
            self.call("GET", f"/api/logs/{made[3]['id']}", expect=404)

    def test_one_file_at_a_time(self):
        self.support._busy.acquire()
        try:
            self.assertIn("gerade schon", self.call("POST", "/api/logs/prepare", expect=409)["error"])
        finally:
            self.support._busy.release()

    def test_a_heater_that_goes_away_is_noted_once(self):
        self.heater.online = False
        self.service.poll_all()
        self.service.poll_all()
        self.heater.online = True
        self.service.poll_all()
        text = logs.read_all()
        self.assertRegex(text, r"INFO {4}service  Heizung Heat4U-911001 \(127\.0\.0\.1:\d+\) erreichbar: 21,5 °C, "
                               r"Soll 20,0 °C, Manuell · Firmware 00\.01\.08 · Seriennummer 91110001\n")
        self.assertEqual(text.count("antwortet nicht"), 1)
        self.assertIn("antwortet wieder", text)
        self.assertNotIn(TOKEN, text)


# ---------------------------------------------------------------------- the Pi's system logs (install.py --logs)
class FakePi(install.DryRun):
    """The programs of a Raspberry Pi; what they print holds the secrets that must not leave it."""

    def __init__(self):
        super().__init__()
        self.missing = {"nmcli", "tailscale"}
        self.outputs = {
            ("vcgencmd", "get_throttled"): "throttled=0x50005\n",
            ("ip", "-brief", "address"): "lo               UNKNOWN        127.0.0.1/8\nwlan0            UP             192.168.1.50/24\n",
            ("journalctl", "--list-boots"): "IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY\n"
                                            + "".join(f"{-i:3d} {i:032x} Sat 2026-11-{i % 28 + 1:02d} …\n" for i in range(30)),
            ("journalctl", "-u", "flowthermpi"):
                "2026-11-22T10:00:00+01:00 python3[512]: Traceback (most recent call last):\n"
                f"2026-11-22T10:00:00+01:00 python3[512]: urlopen coap://192.168.1.10/Json?token={TOKEN}\n"
                f"2026-11-22T10:00:01+01:00 python3[512]: Umgebung: HEIZAPP_PASSWORD={APP_PASSWORD}\n",
            ("journalctl", "-u", "flowthermpi-remote"):
                "2026-11-22T10:05:00+01:00 python3[901]: To authenticate, visit:\n"
                f"2026-11-22T10:05:00+01:00 python3[901]: \thttps://login.tailscale.com/a/{LOGIN}\n",
            ("journalctl", "-u", "flowthermpi-zigbee2mqtt"):
                f"2026-11-22T10:06:00+01:00 node[990]: MQTT: Anmeldung von zigbee2mqtt mit {Z2M_MQTT} abgelehnt\n",
            ("journalctl", "-u", "mosquitto"):
                f"2026-11-22T10:06:00+01:00 mosquitto[700]: Client flowthermpi ({APP_MQTT}) abgelehnt\n",
            ("journalctl", "-u", "NetworkManager"):
                f"2026-11-22T09:00:00+01:00 NetworkManager[401]: secrets: psk {WLAN}\n"
                f"2026-11-22T09:00:01+01:00 NetworkManager[401]: 802-11-wireless-security.psk:   {OTHER_WLAN}\n"
                "2026-11-22T09:00:02+01:00 NetworkManager[401]: device (wlan0): Activation: successful\n",
            ("journalctl", "-k"): "2026-11-22T08:00:00+01:00 kernel: hwmon hwmon1: Undervoltage detected!\n",
        }

    def execute(self, cmd, env, timeout=None):
        self.commands.append(cmd)
        if cmd[0] in self.missing:
            raise FileNotFoundError(cmd[0])
        for start, answer in self.outputs.items():
            if cmd[:len(start)] == start:
                return 0, answer, ""
        return 0, "", ""

    def journal(self):
        return [c for c in self.commands if c[:2] == ("journalctl", "-u")]


def logs_pi_root(root: Path, outside: Path) -> Path:
    """The files of a Raspberry Pi that „install.py --logs“ reads – with the secrets it has to find. Returns the data
    folder. outside: where a planted link in the data folder points to."""
    def write(live: str, text: str) -> None:
        path = root / live.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    data = root / install.DATA_DIR.lstrip("/")
    write(f"{install.DATA_DIR}/data/logs/flowthermpi.log", "2026-11-22 10:00:00.000 +0100 INFO …\n")
    write(f"{install.DATA_DIR}/heizungen.conf", f"# Heizungen\n192.168.1.10 {TOKEN}\n192.168.1.11\n")
    write(f"{install.DATA_DIR}/{install.ZIGBEE_LOGIN}", json.dumps({"host": "127.0.0.1", "port": 1883,
                                                                  "user": "flowthermpi", "password": APP_MQTT}))
    (data / "fremd").symlink_to(outside)
    write(install.ZIGBEE_ENV, f"ZIGBEE2MQTT_CONFIG_MQTT_USER=zigbee2mqtt\nZIGBEE2MQTT_CONFIG_MQTT_PASSWORD={Z2M_MQTT}\n")
    write("/etc/default/flowthermpi", f'HEIZAPP_PASSWORD="{APP_PASSWORD}"\n')
    write("/etc/NetworkManager/system-connections/Zuhause.nmconnection",
          f"[connection]\nid=Zuhause\ntype=wifi\n\n[wifi]\nssid=Zuhause\n\n[wifi-security]\nkey-mgmt=wpa-psk\npsk={WLAN}\n")
    write("/proc/uptime", "90061.5 300000.0\n")    # 1 day, 1 hour, 1 minute
    write("/proc/loadavg", "0.15 0.10 0.05 1/180 4242\n")
    write("/proc/meminfo", "MemTotal:        3884332 kB\nMemAvailable:    3012345 kB\n"
                           "SwapTotal:        204796 kB\nSwapFree:         204796 kB\n")
    write("/proc/device-tree/model", "Raspberry Pi 4 Model B Rev 1.5\x00")
    write("/sys/class/thermal/thermal_zone0/temp", "48312\n")
    for name, value in {"idVendor": "10c4", "idProduct": "ea60", "manufacturer": "ITEAD",
                        "product": "SONOFF Zigbee 3.0 USB Dongle Plus V2", "serial": "8a2c1d0e9f"}.items():
        write(f"/sys/bus/usb/devices/1-1.3/{name}", value + "\n")
    return data


class SystemLogBase(LogBase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        self.data = logs_pi_root(self.root, self.tmp)
        self.pi = FakePi()
        self.said = []
        patcher = mock.patch.object(install, "say", lambda text="": self.said.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, text: str | None = None) -> dict:
        """As flowthermpi-logs.service does it when the app has placed its request."""
        (self.data / install.LOGS_REQUEST).write_text(text or json.dumps({"id": "0123abcd", "action": "collect"}))
        self.assertEqual(install.main(["--root", str(self.root), "--logs"], self.pi), 0)
        self.assertFalse((self.data / install.LOGS_REQUEST).exists())
        path = self.data / install.LOGS_STATUS
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        return json.loads(path.read_text())

    def report(self) -> str:
        path = self.data / install.LOGS_FILE
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        return path.read_text()

    def start_root(self):
        """The root service: runs the real install.py --logs whenever the app places a request."""
        stop = threading.Event()
        args = install.parse_args(["--root", str(self.root), "--logs"])

        def service():
            while not stop.is_set():
                if (self.data / install.LOGS_REQUEST).exists():
                    install.logs_from_app(args, self.pi)
                time.sleep(0.005)

        thread = threading.Thread(target=service, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)


class LogsFromAppTest(SystemLogBase):
    """install.py --logs, as flowthermpi-logs.service runs it for „Protokoll speichern“."""

    def test_everything_the_helper_needs_and_no_secret(self):
        status = self.ask()
        report = self.report()
        self.assertEqual((status["id"], status["state"], status["size"]), ("0123abcd", "done", len(report.encode())))
        self.assertTrue(status["finished"])
        for secret in SECRETS:
            self.assertNotIn(secret, report)
        self.assertIn("Json?token=•••", report)
        self.assertIn("HEIZAPP_PASSWORD=•••", report)
        self.assertIn("https://login.tailscale.com/a/•••", report)
        self.assertIn("secrets: psk •••", report)
        self.assertIn("802-11-wireless-security.psk:   •••", report)
        self.assertIn("mit ••• abgelehnt", report)
        self.assertIn("Client flowthermpi (•••) abgelehnt", report)
        self.assertIn("device (wlan0): Activation: successful", report)          # the rest stays readable
        self.assertTrue(report.startswith("FlowThermPi – Systemprotokolle vom "))
        self.assertIn("Raspberry Pi 4 Model B Rev 1.5", report)
        self.assertIn("1 Tg. 1 Std. 1 Min. (Last 0.15 0.10 0.05)", report)
        self.assertIn("2941 von 3793 MB frei", report)
        self.assertIn("CPU-Temperatur:   48,3 °C", report)
        self.assertIn("0x50005 – Unterspannung jetzt, gedrosselt jetzt, Unterspannung seit dem Start, "
                      "gedrosselt seit dem Start", report)
        self.assertIn("Journal:          nur seit dem letzten Start", report)
        self.assertIn("Tailscale:        nicht installiert", report)
        self.assertIn("1-1.3: 10c4:ea60 ITEAD SONOFF Zigbee 3.0 USB Dongle Plus V2", report)
        self.assertNotIn("8a2c1d0e9f", report)                                    # no serial numbers
        self.assertIn("(nmcli gibt es auf diesem Pi nicht)", report)
        self.assertIn("Undervoltage detected!", report)
        listing = report.split("===== Datenordner /var/lib/flowthermpi =====\n")[1].split("\n=====")[0]
        self.assertRegex(listing, r"(?m)^heizungen\.conf  \d+ B  -rw")          # names and sizes, not contents
        self.assertRegex(listing, r"(?m)^data/ .*\n  logs/ .*\n    flowthermpi\.log  \d+ B")
        self.assertRegex(listing, r"(?m)^fremd → Verweis ")                     # a link is named, never followed
        boots = report.split("===== Systemstarts =====\n")[1].split("\n=====")[0].splitlines()
        self.assertEqual((boots[0][:3], boots[1], len(boots)), ("IDX", "…", 22))   # the latest 20 starts
        units = self.pi.journal()
        self.assertEqual([c[2] for c in units], [unit for unit, _ in install.LOGS_UNITS])
        self.assertEqual(units[0][3:5], ("-n", "3000"))
        self.assertTrue(any(arg.startswith("--since=") for arg in units[0]))
        self.assertFalse((self.tmp / install.LOGS_FILE).exists())

    def test_files_and_requests_that_are_links_are_not_followed(self):
        outside = self.tmp / "fremde-datei"
        outside.write_text("bleibt\n")
        (self.data / install.LOGS_FILE).symlink_to(outside)
        self.assertEqual(self.ask()["state"], "done")
        self.assertFalse((self.data / install.LOGS_FILE).is_symlink())   # replaced, not written through
        self.assertEqual(outside.read_text(), "bleibt\n")
        planted = self.tmp / "anfrage.json"
        planted.write_text('{"action": "collect"}')
        (self.data / install.LOGS_REQUEST).symlink_to(planted)
        self.assertEqual(install.main(["--root", str(self.root), "--logs"], self.pi), 1)
        self.assertFalse((self.data / install.LOGS_REQUEST).is_symlink())

    def test_the_request_cannot_ask_for_anything_else(self):
        for text, why in (('{"id": "0123abcd", "action": "rm"}', "Unbekannte Anfrage"), ("kaputt", "unlesbar")):
            status = self.ask(text)
            self.assertEqual(status["state"], "failed")
            self.assertIn(why, status["message"])
            self.assertFalse((self.data / install.LOGS_FILE).exists())
        self.assertEqual(self.pi.journal(), [])

    def test_nothing_to_do_without_request(self):
        self.assertEqual(install.main(["--root", str(self.root), "--logs"], self.pi), 0)
        self.assertFalse((self.data / install.LOGS_STATUS).exists())
        self.assertEqual(self.pi.commands, [])

    def test_a_very_long_report_is_cut_in_the_middle(self):
        self.pi.outputs[("journalctl", "-k")] = "".join(f"kernel: Zeile {i:06d} {'x' * 40}\n" for i in range(20000))
        with mock.patch.object(install, "LOGS_MAX", 64 << 10):
            self.assertEqual(self.ask()["state"], "done")
        report = self.report()
        self.assertLess(len(report.encode()), (64 << 10) + 100)
        self.assertTrue(report.startswith("FlowThermPi – Systemprotokolle vom "))   # the overview stays …
        self.assertIn("… (zu lang – hier gekürzt) …", report)
        self.assertTrue(report.rstrip().endswith("Zeile 019999 " + "x" * 40))      # … and the newest lines


class SystemLogAppTest(SystemLogBase):
    """The app's side: it asks the root service and puts the answer into the file."""

    def test_the_file_holds_the_system_logs(self):
        self.start_root()
        conf = self.tmp / "heizungen.conf"
        conf.write_text("")
        service = Service(str(conf), str(self.tmp / "data.json"), poll_interval=3600)
        helper = Support(service, syslog_dir=self.data, poll=0.01)
        made = helper.prepare()
        self.assertEqual((made["system"], made["missing"]), (True, None))
        with zipfile.ZipFile(io.BytesIO(helper.download(made["id"]).data)) as archive:
            system = archive.read("system.txt").decode()
            readme = archive.read("LIESMICH.txt").decode()
        self.assertIn("===== Überblick =====", system)
        self.assertIn("Undervoltage detected!", system)
        for secret in SECRETS:
            self.assertNotIn(secret, system)
        self.assertNotIn("fehlen diesmal", readme)
        self.assertFalse((self.data / install.LOGS_FILE).exists())      # read and removed by the app
        asked = len(self.pi.journal())
        again, why = helper.syslog.collect()                              # pressed again: root is not asked again
        self.assertIsNone(why)
        self.assertIn("===== Überblick =====", again)
        self.assertEqual(len(self.pi.journal()), asked)

    def test_without_the_root_service_the_file_says_why(self):
        syslog = SystemLog(self.data, poll=0.01)
        syslog.pickup = 0.2
        text, why = syslog.collect()
        self.assertIsNone(text)
        self.assertIn("reagiert nicht", why)
        self.assertFalse((self.data / support.REQUEST).exists())          # taken back
        self.assertIn("reagiert nicht", logs.read_all())

    def test_a_failure_on_the_pi_is_passed_on(self):
        with mock.patch.object(install, "system_report", side_effect=install.InstallError("Kein Platz mehr.")):
            self.start_root()
            self.assertEqual(SystemLog(self.data, poll=0.01).collect(), (None, "Kein Platz mehr."))
        self.assertFalse((self.data / install.LOGS_FILE).exists())


class LogsInstallerTest(InstallBase):
    """The installer sets up the root service behind „Protokoll speichern“."""

    def test_fresh_install_has_the_logs_units(self):
        system, out = self.run_install()
        self.assertIn("Environment=HEIZAPP_SYSLOG_DIR=/var/lib/flowthermpi\n", self.path(install.UNIT).read_text())
        self.assertIn("PathExists=/var/lib/flowthermpi/logs-request.json\n", self.path(install.LOGS_PATH).read_text())
        unit = self.path(install.LOGS_UNIT).read_text()
        self.assertIn("/opt/flowthermpi/current/install.py --logs\n", unit)
        self.assertIn("StartLimitBurst=30\n", unit)
        self.assertIn("systemctl enable --now flowthermpi-logs.path", [" ".join(c) for c in system.commands])
        self.assertIn("Protokoll speichern", out)

    def test_uninstall_removes_them(self):
        self.run_install()
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(install.main(["--root", str(self.root), "--uninstall"], install.DryRun()), 0)
        self.assertFalse(self.path(install.LOGS_UNIT).exists() or self.path(install.LOGS_PATH).exists())


if __name__ == "__main__":
    unittest.main()
