"""Tests for Einrichtung › Zigbee: the root part (install.py --zigbee) and heizapp.zigbee.Zigbee with its API routes.

Nothing is downloaded and nothing is run: FakePi plays dpkg, apt, Mosquitto, systemd, Node.js, pnpm and the service of
Zigbee2MQTT, and the downloads are small archives made here – with their real checksums, so the checks of install.py
run for real. Zigbee2MQTT itself, behind Mosquitto, is played by tests/fake_mqtt.py.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import install  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError, Service  # noqa: E402
from heizapp.zigbee import Zigbee  # noqa: E402
from fake_mqtt import FakeBroker, FakeZ2M, thermostat  # noqa: E402
from test_install import Base as InstallBase  # noqa: E402

COMMIT = "3c841fdfd66c92239921ef84fde8f039c64e1662"
STICK = "usb-SONOFF_SONOFF_Dongle_Plus_MG24_8a2c1d0e-if00-port0"


def tar_gz(entries, comment: str | None = None) -> bytes:
    """entries: ("d", name) | ("f", name, data[, mode]) | ("l", name, target), in this order."""
    buf = io.BytesIO()
    headers = {"comment": comment} if comment else None
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=headers) as tar:
        for kind, name, *rest in entries:
            info = tarfile.TarInfo(name)
            if kind == "d":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif kind == "l":
                info.type, info.linkname = tarfile.SYMTYPE, rest[0]
                tar.addfile(info)
            else:
                data = rest[0]
                info.size, info.mode = len(data), rest[1] if len(rest) > 1 else 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def node_archive(version: str = "24.22.3") -> bytes:
    top = f"node-v{version}-linux-arm64"
    return tar_gz([("d", top), ("f", f"{top}/bin/node", b"\x7fELF node", 0o755),
                   ("f", f"{top}/lib/node_modules/npm/bin/npm-cli.js", b"// npm", 0o755),
                   ("l", f"{top}/bin/npm", "../lib/node_modules/npm/bin/npm-cli.js")])


def pnpm_archive() -> bytes:
    return tar_gz([("f", "package/package.json", b'{"name": "pnpm", "version": "11.26.0"}'),
                   ("f", "package/bin/pnpm.mjs", b"// pnpm", 0o755)])


def z2m_archive(version: str = "2.14.3", commit: str = COMMIT, comment: str | None = None) -> bytes:
    top = f"zigbee2mqtt-{commit}"
    return tar_gz([("d", top), ("f", f"{top}/package.json", json.dumps({"name": "zigbee2mqtt", "version": version}).encode()),
                   ("f", f"{top}/pnpm-lock.yaml", b"lockfileVersion: '9.0'\n"), ("f", f"{top}/index.js", b"// start")],
                  comment=comment or commit)


class FakePi(install.DryRun):
    """dpkg, apt, Mosquitto, systemd and the programs as on a Raspberry Pi with Raspberry Pi OS 64-bit."""

    run = install.System.run     # the real error handling

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.arch = "arm64"
        self.mosquitto = None         # installed version of the package
        self.mosquitto_active = False
        self.users: set[str] = set()
        self.apt_error = None         # what „apt-get install“ says when it fails
        self.fail: dict[str, tuple[int, str]] = {}   # "install" | "build": what pnpm answers instead
        self.hashed: list[list[str]] = []            # what mosquitto_passwd found in its files
        self.chowned: list[tuple[Path, str]] = []
        self.as_user: list[tuple[str, tuple, Path, dict]] = []
        self.slow = 0.0               # seconds pnpm takes (for the app's tests)
        self.z2m = {"enabled": False, "active": False, "failed": False, "restarts": 0}   # Zigbee2MQTT's service
        self.z2m_fails = False        # it ends at once after every start (no stick, broken settings)
        self.z2m_journal = ""         # what journalctl shows of it
        self.on_z2m = None            # on_z2m(True | False) when it starts or stops: the simulated Zigbee2MQTT

    def user_exists(self, user: str = install.NAME) -> bool:
        return user in self.users

    def lchown(self, path, user):
        self.chowned.append((Path(path), user))

    def chown_tree(self, top, user):
        self.chowned.append((Path(top), user))

    def ok(self, *cmd):
        self.commands.append(cmd)
        if cmd == ("systemctl", "is-enabled", "--quiet", install.ZIGBEE_SERVICE):
            return self.z2m["enabled"]
        return cmd == ("systemctl", "is-active", "--quiet", "mosquitto") and self.mosquitto_active

    def zigbee2mqtt(self, args):
        """systemctl for Zigbee2MQTT's service."""
        z, verb = self.z2m, args[0]
        if verb == "show":
            state = "active" if z["active"] else "failed" if z["failed"] else "inactive"
            sub = {"active": "running", "failed": "failed", "inactive": "dead"}[state]
            return 0, (f"NRestarts={z['restarts']}\nActiveState={state}\nSubState={sub}\n"
                       f"UnitFileState={'enabled' if z['enabled'] else 'disabled'}\n"), ""
        if verb in ("enable", "restart", "start") and not (self.root / install.ZIGBEE_SERVICE_UNIT.lstrip("/")).exists():
            return 5, "", f"Failed to {verb} {install.ZIGBEE_SERVICE}.service: Unit not found.\n"
        if verb in ("enable", "disable"):
            z["enabled"] = verb == "enable"
            if "--now" in args:
                self.z2m_run(verb == "enable")
        elif verb in ("restart", "try-restart"):
            if verb == "restart" or z["active"]:
                self.z2m_run(False)
                self.z2m_run(True)
        elif verb == "stop":
            self.z2m_run(False)
        elif verb == "reset-failed":
            z["failed"] = False
        return 0, "", ""

    def z2m_run(self, on):
        z = self.z2m
        if on and self.z2m_fails:
            z.update(active=False, failed=True, restarts=z["restarts"] + 3)
            return
        was = z["active"]
        z.update(active=on, failed=False)
        if self.on_z2m and was != on:
            self.on_z2m(on)

    def execute(self, cmd, env, timeout=None):
        self.commands.append(cmd)
        if cmd == ("dpkg", "--print-architecture"):
            return 0, self.arch + "\n", ""
        if cmd[0] == "dpkg-query":
            if self.mosquitto and cmd[-1] == "mosquitto":
                return 0, f"install ok installed {self.mosquitto}", ""
            return 1, "", f"dpkg-query: no packages found matching {cmd[-1]}\n"
        if cmd[0] == "apt-get":
            assert env and env["DEBIAN_FRONTEND"] == "noninteractive", env
            if "install" in cmd:
                if self.apt_error:
                    return 100, "Reading package lists...\n", self.apt_error
                assert cmd[-2:] == ("--no-install-recommends", "mosquitto"), cmd
                self.mosquitto, self.mosquitto_active = "2.0.21-1", True
                (self.root / "etc/mosquitto/conf.d").mkdir(parents=True, exist_ok=True)
            if "purge" in cmd:
                self.mosquitto, self.mosquitto_active = None, False
            return 0, "", ""
        if cmd[:2] == ("mosquitto_passwd", "-U"):
            path = Path(cmd[2])
            assert oct(path.stat().st_mode & 0o777) == "0o600"
            lines = path.read_text().splitlines()
            self.hashed.append(lines)
            path.write_text("".join(f"{line.split(':')[0]}:$7$101$c2FsdA==$aGFzaA==\n" for line in lines))
            return 0, "", ""
        if cmd[0] == "systemctl" and install.ZIGBEE_SERVICE in cmd:
            return self.zigbee2mqtt(tuple(c for c in cmd[1:] if c != install.ZIGBEE_SERVICE))
        if cmd[0] == "journalctl":
            return 0, self.z2m_journal, ""
        if cmd[0] == "systemctl":
            if cmd[1:] == ("restart", "mosquitto"):
                if not self.mosquitto:
                    return 5, "", "Failed to restart mosquitto.service: Unit mosquitto.service not found.\n"
                self.mosquitto_active = True
            return 0, "", ""
        if cmd[0] == "useradd":
            self.users.add(cmd[-1])
            return 0, "", ""
        if cmd[0] == "userdel":
            self.users.discard(cmd[-1])
            return 0, "", ""
        return 127, "", f"{cmd[0]}: im Test unbekannt"

    def run_as(self, user, cmd, cwd, env, timeout):
        self.as_user.append((user, cmd, Path(cwd), env))
        assert user == install.ZIGBEE_USER and user in self.users, user
        if cmd[-1] == "--version":
            program = Path(cmd[1] if len(cmd) == 3 else cmd[0])
            if not (program.is_file() and Path(cmd[0]).is_file()):
                return 127, f"{program}: not found"
            folder = program.parents[1].name   # node-v24.22.3 | pnpm-11.26.0
            return 0, folder.removeprefix("node-").removeprefix("pnpm-") + "\n"
        args = cmd[2:]
        if args[:2] == ("install", "--frozen-lockfile"):
            time.sleep(self.slow)
            if "install" in self.fail:
                return self.fail["install"]
            (Path(cwd) / "node_modules" / ".pnpm").mkdir(parents=True)
            return 0, "Lockfile is up to date, resolution step is skipped\nPackages: +612\nDone in 3m 12.4s\n"
        if args == ("run", "build"):
            time.sleep(self.slow)
            if "build" in self.fail:
                return self.fail["build"]
            dist = Path(cwd) / "dist"
            dist.mkdir()
            (dist / ".hash").write_text("unknown")
            (dist / "controller.js").write_text("// built")
            return 0, "> zigbee2mqtt@2.14.3 build\n> tsc && node index.js writehash\n"
        return 127, f"{cmd}: im Test unbekannt"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.data = self.root / install.DATA_DIR.lstrip("/")
        self.data.mkdir(parents=True)
        self.base = self.root / install.ZIGBEE_DIR.lstrip("/")
        self.pi = FakePi(self.root)
        self.files = {}
        self.set_parts(node_archive(), pnpm_archive(), z2m_archive())
        self.fetched, self.said, self.reports = [], [], []
        original = install.write_private

        def spy(folder, name, text, system):
            if name == install.ZIGBEE_STATUS:
                self.reports.append(json.loads(text))
            return original(folder, name, text, system)

        for patcher in (mock.patch.object(install, "fetch_file", self.fake_fetch),
                        mock.patch.object(install, "say", lambda text="": self.said.append(text)),
                        mock.patch.object(install, "free_mb", lambda path: 20_000),
                        mock.patch.object(install, "write_private", spy)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_parts(self, node, pnpm, z2m, version="2.14.3", commit=COMMIT, node_version="24.22.3"):
        """The archives „on the internet“ and ZIGBEE_PARTS with their checksums."""
        parts = {"node": {"version": node_version, "sha256": {"arm64": hashlib.sha256(node).hexdigest(), "amd64": "0" * 64}},
                 "pnpm": {"version": "11.26.0", "sha512": hashlib.sha512(pnpm).hexdigest()},
                 "zigbee2mqtt": {"version": version, "commit": commit}}
        patcher = mock.patch.object(install, "ZIGBEE_PARTS", parts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.files.update({install.NODE_URL.format(version=node_version, arch="arm64"): node,
                           install.PNPM_URL.format(version="11.26.0"): pnpm,
                           install.ZIGBEE2MQTT_URL.format(commit=commit): z2m})

    def fake_fetch(self, url, dest, report=None, limit=0):
        self.fetched.append(url)
        if url not in self.files:
            raise install.InstallError(f"{url} gibt es nicht (mehr).")
        if report:
            report(50)
            report(100)
        Path(dest).write_bytes(self.files[url])

    def ask(self, action):
        text = action if action.startswith("{") or action == "kaputt" else json.dumps({"id": "0123abcd", "action": action})
        (self.data / install.ZIGBEE_REQUEST).write_text(text)
        code = install.main(["--root", str(self.root), "--zigbee"], self.pi)
        self.assertEqual(code, 0)
        self.assertFalse((self.data / install.ZIGBEE_REQUEST).exists())
        path = self.data / install.ZIGBEE_STATUS
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        return json.loads(path.read_text())

    def path(self, live):
        return self.root / live.lstrip("/")

    def plug(self, name=STICK):
        """The stick in the Pi: its lasting name under /dev/serial/by-id."""
        by_id = self.root / "dev/serial/by-id"
        by_id.mkdir(parents=True, exist_ok=True)
        (by_id / name).symlink_to("../../ttyUSB0")

    def setup_request(self, channel=25, port=STICK):
        return json.dumps({"id": "0123abce", "action": "setup", "channel": channel, "port": port})

    def z2m_file(self, name):
        return self.path(install.ZIGBEE_DATA) / name

    def logins(self):
        app = json.loads((self.data / install.ZIGBEE_LOGIN).read_text())
        env = dict(line.split("=", 1) for line in self.path(install.ZIGBEE_ENV).read_text().splitlines()
                   if line and not line.startswith("#"))
        return app, env


class ZigbeeFromAppTest(Base):
    """install.py --zigbee, as flowthermpi-zigbee.service runs it for Einrichtung › Zigbee."""

    def test_install_brings_everything(self):
        status = self.ask("install")
        self.assertEqual((status["state"], status["action"], status["id"]), ("done", "install", "0123abcd"), status)
        self.assertEqual(status["message"], "Die Zigbee-Programme sind installiert.")
        phases = [r["phase"] for r in self.reports if r["phase"]]
        self.assertEqual(list(dict.fromkeys(phases)), list(install.ZIGBEE_STEPS))   # the app can follow each step
        self.assertIn(50, [r["progress"] for r in self.reports if r["phase"] == "node"])
        self.assertEqual(self.fetched, list(self.files))                            # Node.js, pnpm, Zigbee2MQTT
        # Mosquitto from Debian: only on this Pi, only with a password
        self.assertTrue([c for c in self.pi.commands if c[:1] == ("apt-get",) and "install" in c])
        conf = self.path(install.MOSQUITTO_CONF).read_text()
        self.assertIn("listener 1883 127.0.0.1\nallow_anonymous false\npassword_file /etc/mosquitto/flowthermpi.passwd\n", conf)
        self.assertEqual([[line.split(":")[0] for line in lines] for lines in self.pi.hashed], [["flowthermpi", "zigbee2mqtt"]])
        passwd = self.path(install.MOSQUITTO_PASSWD)
        self.assertNotIn(self.logins()[0]["password"], passwd.read_text())          # hashes only
        self.assertEqual(oct(passwd.stat().st_mode & 0o777), "0o600")
        self.assertEqual([u for p, u in self.pi.chowned if p.name.startswith(f".{passwd.name}.")],
                         ["mosquitto"])                                            # Mosquitto may read it again
        # the logins: the app's in its own folder, Zigbee2MQTT's only for root
        app, env = self.logins()
        self.assertEqual((app["host"], app["port"], app["user"]), ("127.0.0.1", 1883, "flowthermpi"))
        self.assertEqual(oct((self.data / install.ZIGBEE_LOGIN).stat().st_mode & 0o777), "0o600")
        self.assertEqual((env["ZIGBEE2MQTT_CONFIG_MQTT_SERVER"], env["ZIGBEE2MQTT_CONFIG_MQTT_USER"]),
                         ("mqtt://127.0.0.1:1883", "zigbee2mqtt"))
        self.assertEqual(oct(self.path(install.ZIGBEE_ENV).stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.path(install.ZIGBEE_ENV).parent.stat().st_mode & 0o777), "0o700")
        self.assertNotEqual(app["password"], env["ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD"])
        # the programs: checked, unpacked with their links, built by their own user
        for link, target in (("node", "node-v24.22.3"), ("pnpm", "pnpm-11.26.0"), ("zigbee2mqtt", "zigbee2mqtt-2.14.3-1")):
            self.assertEqual(str((self.base / link).readlink()), target)
        self.assertEqual(oct((self.base / "node/bin/node").stat().st_mode & 0o777), "0o755")
        self.assertEqual(str((self.base / "node/bin/npm").readlink()), "../lib/node_modules/npm/bin/npm-cli.js")
        self.assertIn((self.base / "zigbee2mqtt-2.14.3-1", install.ZIGBEE_USER), self.pi.chowned)
        self.assertIn(install.ZIGBEE_USER, self.pi.users)
        self.assertEqual(oct(self.path(install.ZIGBEE_DATA).stat().st_mode & 0o777), "0o700")
        pnpm = [(cmd[2:], cwd, env) for _, cmd, cwd, env in self.pi.as_user if cmd[-1] != "--version"]
        self.assertEqual([args[:2] for args, _, _ in pnpm], [("install", "--frozen-lockfile"), ("run", "build")])
        self.assertEqual({cwd for _, cwd, _ in pnpm}, {self.base / "zigbee2mqtt-2.14.3-1"})
        self.assertTrue(pnpm[0][2]["PATH"].startswith(f"{self.base}/node/bin:"))   # our Node.js, not the system's
        self.assertEqual(pnpm[0][2]["HOME"], str(self.base / "work"))
        info = json.loads((self.base / install.ZIGBEE_INFO).read_text())
        self.assertEqual((info["node"], info["pnpm"], info["zigbee2mqtt"], info["commit"], info["complete"], info["mosquitto_ours"]),
                         ("24.22.3", "11.26.0", "2.14.3", COMMIT, True, True))
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),
                         ["install.lock", "installed.json", "node", "node-v24.22.3", "pnpm", "pnpm-11.26.0", "work",
                          "zigbee2mqtt", "zigbee2mqtt-2.14.3-1"])          # no download left behind
        z = status["zigbee"]
        self.assertEqual((z["arch"], z["arch_ok"], z["complete"], z["installed"], z["wanted"]),
                         ("arm64", True, True, {"node": "24.22.3", "pnpm": "11.26.0", "zigbee2mqtt": "2.14.3"},
                          {"node": "24.22.3", "pnpm": "11.26.0", "zigbee2mqtt": "2.14.3"}))
        self.assertEqual(z["mosquitto"], {"version": "2.0.21-1", "active": True})
        self.assertEqual(z["service"], {"present": False, "enabled": False, "active": False, "failed": False,
                                        "restarts": 0, "log": None})   # Zigbee2MQTT runs once it is set up
        self.assertEqual(z["network"], {"configured": False, "channel": None, "port": None, "devices": 0})

    def test_passwords_are_never_shown(self):
        self.ask("install")
        app, env = self.logins()
        secrets = [app["password"], env["ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD"]]
        shown = [json.dumps(self.reports), "\n".join(self.said), json.dumps(self.pi.commands, default=str),
                 json.dumps([cmd for _, cmd, _, _ in self.pi.as_user]), (self.base / install.ZIGBEE_INFO).read_text(),
                 self.path(install.MOSQUITTO_CONF).read_text()]
        for secret in secrets:
            self.assertGreaterEqual(len(secret), 30)
            for text in shown:
                self.assertNotIn(secret, text)

    def test_again_only_renews_the_logins(self):
        self.ask("install")
        before = self.logins()
        self.fetched.clear()
        apt = len([c for c in self.pi.commands if c[:1] == ("apt-get",)])
        status = self.ask("install")
        self.assertEqual(status["state"], "done")
        self.assertEqual(self.fetched, [])                                               # nothing loaded again
        self.assertEqual(len([c for c in self.pi.commands if c[:1] == ("apt-get",)]), apt)   # Mosquitto stays
        self.assertNotEqual(self.logins(), before)                                       # new passwords
        self.assertEqual(str((self.base / "zigbee2mqtt").readlink()), "zigbee2mqtt-2.14.3-1")
        self.assertTrue(json.loads((self.base / install.ZIGBEE_INFO).read_text())["mosquitto_ours"])

    def test_a_newer_version_replaces_the_old_one(self):
        self.ask("install")
        self.set_parts(node_archive("24.23.0"), pnpm_archive(), z2m_archive("2.15.0", "b" * 40), version="2.15.0",
                       commit="b" * 40, node_version="24.23.0")
        status = self.ask("install")
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(str((self.base / "zigbee2mqtt").readlink()), "zigbee2mqtt-2.15.0-2")
        self.assertEqual(str((self.base / "node").readlink()), "node-v24.23.0")
        self.assertFalse((self.base / "zigbee2mqtt-2.14.3-1").exists())   # the old versions are gone
        self.assertFalse((self.base / "node-v24.22.3").exists())
        self.assertEqual(status["zigbee"]["installed"], {"node": "24.23.0", "pnpm": "11.26.0", "zigbee2mqtt": "2.15.0"})

    def test_a_failed_build_keeps_what_worked(self):
        self.ask("install")
        self.set_parts(node_archive(), pnpm_archive(), z2m_archive("2.15.0", "b" * 40), version="2.15.0", commit="b" * 40)
        self.pi.fail["install"] = (1, "Progress: resolved 612\n ERR_PNPM_ENOSPC  ENOSPC: no space left on device, write\n")
        status = self.ask("install")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["message"], "Die Bausteine von Zigbee2MQTT ließen sich nicht laden (die Speicherkarte ist voll).")
        self.assertIn("ENOSPC: no space left on device", status["details"])          # for „Meldungen des Pi“
        self.assertEqual(str((self.base / "zigbee2mqtt").readlink()), "zigbee2mqtt-2.14.3-1")   # still in use
        self.assertFalse((self.base / "zigbee2mqtt-2.15.0-2").exists())             # the half one is gone
        self.assertEqual(status["zigbee"]["installed"]["zigbee2mqtt"], "2.14.3")
        self.assertTrue(status["zigbee"]["complete"])

    def test_reasons_in_words(self):
        for out, code, reason in (("FATAL ERROR: Reached heap limit - JavaScript heap out of memory", 134, "zu wenig Arbeitsspeicher"),
                                  ("request to https://registry.npmjs.org/x failed, reason: getaddrinfo EAI_AGAIN", 1,
                                   "keine Verbindung zum Internet – bitte später noch einmal versuchen"),
                                  ("…", 124, "es hat zu lange gedauert"), ("tsc: error TS2304", 2, "Fehlercode 2")):
            self.assertEqual(install.pnpm_reason(out, code), reason)

    def test_a_wrong_checksum_stops_everything(self):
        tampered = node_archive()[:-8] + b"\0" * 8
        self.files[install.NODE_URL.format(version="24.22.3", arch="arm64")] = tampered
        status = self.ask("install")
        self.assertEqual(status["state"], "failed")
        self.assertIn("Prüfsumme stimmt nicht", status["message"])
        self.assertFalse((self.base / "node").exists())
        self.assertFalse([p for p in self.base.iterdir() if p.name.startswith(".download-")])
        self.assertFalse([cmd for _, cmd, _, _ in self.pi.as_user if cmd[-1] != "--version"])   # nothing was run

    def test_another_commit_from_github_is_refused(self):
        self.files[install.ZIGBEE2MQTT_URL.format(commit=COMMIT)] = z2m_archive(comment="c" * 40)
        status = self.ask("install")
        self.assertEqual(status["state"], "failed")
        self.assertIn("nicht die erwartete Version von Zigbee2MQTT", status["message"])
        self.assertFalse([p for p in self.base.iterdir() if p.name.startswith("zigbee2mqtt")])

    def test_only_64_bit(self):
        self.pi.arch = "armhf"   # Raspberry Pi OS 32-bit – also on a Pi 4 with its 64-bit kernel
        status = self.ask("install")
        self.assertEqual((status["state"], status["message"]),
                         ("failed", "Für Zigbee braucht der Pi ein 64-Bit-System (Raspberry Pi OS 64-bit) – hier läuft „armhf“."))
        self.assertFalse([c for c in self.pi.commands if c[:1] == ("apt-get",)])
        self.assertEqual((status["zigbee"]["arch"], status["zigbee"]["arch_ok"]), ("armhf", False))

    def test_too_little_space(self):
        with mock.patch.object(install, "free_mb", lambda path: 700):
            status = self.ask("install")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["message"], "Auf der Speicherkarte sind nur noch 700 MB frei – die Zigbee-Programme "
                                            "brauchen etwa 1500 MB.")
        self.assertEqual(self.fetched, [])

    def test_apt_errors_are_reported(self):
        self.pi.apt_error = "E: Unable to locate package mosquitto\n"
        status = self.ask("install")
        self.assertEqual(status["message"], "Mosquitto ließ sich nicht installieren (E: Unable to locate package mosquitto).")

    def test_remove_takes_the_programs_and_keeps_the_data(self):
        self.ask("install")
        (self.path(install.ZIGBEE_DATA) / "database.db").write_text("paired thermostats")
        status = self.ask("remove")
        self.assertEqual((status["state"], status["message"]), ("done", "Die Zigbee-Programme sind entfernt."))
        for gone in (install.ZIGBEE_DIR, install.ZIGBEE_ENV, install.MOSQUITTO_CONF, install.MOSQUITTO_PASSWD):
            self.assertFalse(self.path(gone).exists(), gone)
        self.assertFalse((self.data / install.ZIGBEE_LOGIN).exists())
        self.assertTrue([c for c in self.pi.commands if c[:1] == ("apt-get",) and "purge" in c])   # came with us
        self.assertTrue((self.path(install.ZIGBEE_DATA) / "database.db").exists())   # still paired after a reinstall
        self.assertIn(install.ZIGBEE_USER, self.pi.users)
        self.assertEqual((status["zigbee"]["complete"], status["zigbee"]["mosquitto"]["version"]), (False, None))

    def test_a_mosquitto_from_before_stays(self):
        self.pi.mosquitto, self.pi.mosquitto_active = "2.0.21-1", True
        (self.root / "etc/mosquitto/conf.d").mkdir(parents=True)
        self.ask("install")
        self.assertFalse([c for c in self.pi.commands if c[:1] == ("apt-get",)])
        self.ask("remove")
        self.assertFalse([c for c in self.pi.commands if c[:1] == ("apt-get",)])     # not ours: not removed …
        self.assertEqual(self.pi.mosquitto, "2.0.21-1")
        self.assertIn(("systemctl", "restart", "mosquitto"), self.pi.commands[-6:])   # … only without our settings
        self.assertFalse(self.path(install.MOSQUITTO_CONF).exists())

    def test_status_lists_the_usb_devices(self):
        by_id = self.root / "dev/serial/by-id"
        by_id.mkdir(parents=True)
        (by_id / STICK).symlink_to("../../ttyUSB0")
        status = self.ask("status")
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["zigbee"]["ports"], [{"id": STICK, "device": "ttyUSB0"}])
        self.assertEqual(self.pi.as_user, [])                       # „status“ never installs or runs anything
        self.assertFalse([c for c in self.pi.commands if c[:1] in (("apt-get",), ("useradd",))])

    def test_the_request_cannot_choose_anything(self):
        for text, message in (("kaputt", "Die Anfrage der App zu Zigbee ist unlesbar."),
                              ('{"id": "0123abcd", "action": "shell"}', "Unbekannte Anfrage zu Zigbee."),
                              ('["install"]', "Unbekannte Anfrage zu Zigbee.")):
            status = self.ask(text)
            self.assertEqual((status["state"], status["message"]), ("failed", message))
        self.assertEqual(self.fetched, [])

    def test_nothing_to_do_without_request(self):
        self.assertEqual(install.main(["--root", str(self.root), "--zigbee"], self.pi), 0)
        self.assertFalse((self.data / install.ZIGBEE_STATUS).exists())

    def test_request_link_is_not_followed(self):
        secret = self.tmp / "secret.json"
        secret.write_text('{"action": "install"}')
        (self.data / install.ZIGBEE_REQUEST).symlink_to(secret)
        self.assertEqual(install.main(["--root", str(self.root), "--zigbee"], self.pi), 1)
        self.assertFalse((self.data / install.ZIGBEE_REQUEST).is_symlink())
        self.assertEqual(self.fetched, [])


class SetupTest(Base):
    """„Zigbee einrichten“, „Starten“ and „Anhalten“: install.py --zigbee with the actions setup, start, stop."""

    def setUp(self):
        super().setUp()
        self.ask("install")
        self.plug()

    def test_setup_writes_the_settings_and_the_service(self):
        ran = len(self.pi.as_user)
        status = self.ask(self.setup_request(25))
        self.assertEqual((status["state"], status["action"]), ("done", "setup"), status)
        self.assertEqual(status["message"], "Zigbee2MQTT ist eingerichtet (Funkkanal 25) und startet.")
        config = self.z2m_file("configuration.yaml")
        self.assertEqual(oct(config.stat().st_mode & 0o777), "0o600")
        text = config.read_text()
        for line in ("version: 5\n", "  server: mqtt://127.0.0.1:1883\n", f"  port: '/dev/serial/by-id/{STICK}'\n",
                     "  adapter: ember\n  rtscts: false\n  baudrate: 115200\n", "  channel: 25\n",
                     "  network_key: GENERATE\n  pan_id: GENERATE\n  ext_pan_id: GENERATE\n",
                     "  log_output:\n    - console\n", "  enable_external_js: false\n",
                     "frontend:\n  enabled: false\n", "homeassistant:\n  enabled: false\n", "  retain: true\n"):
            self.assertIn(line, text)
        app, env = self.logins()
        self.assertNotIn(env["ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD"], text)   # only systemd hands it over
        unit = self.path(install.ZIGBEE_SERVICE_UNIT).read_text()
        for line in ("User=flowthermpi-zigbee\n", "SupplementaryGroups=dialout\n",
                     "WorkingDirectory=/opt/flowthermpi-zigbee/zigbee2mqtt\n",
                     "Environment=ZIGBEE2MQTT_DATA=/var/lib/flowthermpi-zigbee\n", "Environment=Z2M_ONBOARD_NO_SERVER=1\n",
                     f"Environment=ZIGBEE2MQTT_CONFIG_SERIAL_PORT=/dev/serial/by-id/{STICK}\n",
                     "EnvironmentFile=/etc/flowthermpi/zigbee2mqtt.env\n",
                     "ExecStart=/opt/flowthermpi-zigbee/node/bin/node index.js\n", "ProtectSystem=strict\n",
                     "ReadWritePaths=/var/lib/flowthermpi-zigbee\n", "Restart=on-failure\n", "WantedBy=multi-user.target\n"):
            self.assertIn(line, unit)
        commands = [" ".join(c) for c in self.pi.commands]
        for command in ("systemctl daemon-reload", "systemctl enable flowthermpi-zigbee2mqtt",
                        "systemctl restart flowthermpi-zigbee2mqtt"):
            self.assertIn(command, commands)
        z = status["zigbee"]
        self.assertEqual((z["service"]["present"], z["service"]["enabled"], z["service"]["active"]), (True, True, True))
        self.assertEqual(z["network"], {"configured": True, "channel": 25, "port": STICK, "devices": 0})
        self.assertEqual(len(self.pi.as_user), ran)   # nothing of Zigbee2MQTT was run here – systemd starts it

    def test_setup_needs_the_programs_and_the_stick(self):
        for request, message in (
                (self.setup_request(30), "Bitte einen Funkkanal von 11 bis 26 wählen."),
                (self.setup_request("25"), "Bitte einen Funkkanal von 11 bis 26 wählen."),
                (self.setup_request(True), "Bitte einen Funkkanal von 11 bis 26 wählen."),
                (self.setup_request(25, "../../etc/passwd"), "Unbekannter Zigbee-Stick."),
                (self.setup_request(25, "usb-Other_Stick-if00-port0"), "Der gewählte Zigbee-Stick steckt nicht (mehr) am Pi.")):
            status = self.ask(request)
            self.assertEqual((status["state"], status["message"]), ("failed", message))
        (self.root / "dev/serial/by-id" / STICK).unlink()
        status = self.ask(self.setup_request(25, None))
        self.assertEqual(status["message"], "Bitte den Zigbee-Stick am Pi einstecken.")
        self.ask("remove")
        self.plug()
        status = self.ask(self.setup_request(25))
        self.assertEqual(status["message"], "Bitte zuerst die Zigbee-Programme installieren.")
        self.assertFalse(self.path(install.ZIGBEE_SERVICE_UNIT).exists())
        self.assertFalse(self.z2m_file("configuration.yaml").exists())

    def test_the_only_usb_device_is_taken(self):
        status = self.ask(self.setup_request(20, None))
        self.assertEqual(status["zigbee"]["network"]["port"], STICK)

    def test_the_same_channel_keeps_the_network(self):
        self.ask(self.setup_request(25))
        # what Zigbee2MQTT made of it at its first start, and a paired thermostat
        written = ("version: 5\nmqtt:\n  base_topic: zigbee2mqtt\n  server: mqtt://127.0.0.1:1883\n  password: x\n"
                   "advanced:\n  channel: 25\n  network_key:\n" + "".join(f"    - {n}\n" for n in range(40, 56)) +
                   "  pan_id: 11079\n")
        self.z2m_file("configuration.yaml").write_text(written)
        self.z2m_file("database.db").write_text('{"id":1,"type":"Coordinator","ieeeAddr":"0x00124b0000000001"}\n'
                                                '{"id":2,"type":"EndDevice","ieeeAddr":"0x8c65a3fffe1d0001"}\n')
        status = self.ask(self.setup_request(25))
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(self.z2m_file("configuration.yaml").read_text(), written)
        self.assertTrue(self.z2m_file("database.db").exists())
        self.assertEqual(status["zigbee"]["network"]["devices"], 1)

    def test_another_channel_only_while_nothing_is_paired(self):
        self.ask(self.setup_request(25))
        database = self.z2m_file("database.db")
        database.write_text('{"id":1,"type":"Coordinator","ieeeAddr":"0x00124b0000000001"}\n'
                            '{"id":2,"type":"EndDevice","ieeeAddr":"0x8c65a3fffe1d0001"}\n'
                            '{"id":3,"type":"Group","groupID":1}\n')
        status = self.ask(self.setup_request(15))
        self.assertEqual(status["state"], "failed")
        self.assertIn("Angelernt: 1", status["message"])
        self.assertIn("  channel: 25\n", self.z2m_file("configuration.yaml").read_text())
        database.write_text('{"id":1,"type":"Coordinator","ieeeAddr":"0x00124b0000000001"}\n')
        self.z2m_file("coordinator_backup.json").write_text("{}")
        status = self.ask(self.setup_request(15))
        self.assertEqual(status["state"], "done", status)
        self.assertIn("  channel: 15\n", self.z2m_file("configuration.yaml").read_text())
        self.assertFalse(database.exists() or self.z2m_file("coordinator_backup.json").exists())   # a new network
        self.assertIn(("systemctl", "stop", install.ZIGBEE_SERVICE), self.pi.commands)

    def test_start_and_stop(self):
        status = self.ask("stop")
        self.assertEqual((status["state"], status["message"]), ("failed", "Zigbee2MQTT ist noch nicht eingerichtet."))
        self.ask(self.setup_request(25))
        status = self.ask("stop")
        self.assertEqual((status["state"], status["message"]), ("done", "Zigbee2MQTT ist angehalten."))
        self.assertIn(("systemctl", "disable", "--now", install.ZIGBEE_SERVICE), self.pi.commands)
        self.assertEqual((status["zigbee"]["service"]["enabled"], status["zigbee"]["service"]["active"]), (False, False))
        before = len(self.pi.commands)
        status = self.ask("start")
        self.assertEqual((status["state"], status["message"]), ("done", "Zigbee2MQTT startet."))
        self.assertEqual(self.pi.commands[before:before + 2], [("systemctl", "reset-failed", install.ZIGBEE_SERVICE),
                                                               ("systemctl", "enable", "--now", install.ZIGBEE_SERVICE)])
        self.assertEqual((status["zigbee"]["service"]["enabled"], status["zigbee"]["service"]["active"]), (True, True))

    def test_a_failing_service_shows_its_messages_without_secrets(self):
        self.ask(self.setup_request(25))
        _, env = self.logins()
        password = env["ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD"]
        self.z2m_file("configuration.yaml").write_text(
            "advanced:\n  channel: 25\n  network_key:\n" + "".join(f"    - {n}\n" for n in range(200, 216)))
        self.pi.z2m_fails = True
        self.pi.z2m_journal = ("\x1b[32mStarting Zigbee2MQTT version 2.14.3\x1b[39m\n"
                               f"mqtt: connecting with password {password}\n"
                               "networkKey: [200,201,202,203,204,205,206,207,208,209,210,211,212,213,214,215]\n"
                               "Error: Failed to open the serial port (no such file)\n"
                               "flowthermpi-zigbee2mqtt.service: Main process exited, code=exited, status=1/FAILURE\n")
        status = self.ask("start")
        self.assertEqual(status["state"], "done")
        service = status["zigbee"]["service"]
        self.assertEqual((service["failed"], service["active"], service["restarts"]), (True, False, 3))
        self.assertIn("Failed to open the serial port", service["log"])
        self.assertNotIn(password, service["log"])
        self.assertNotIn("200,201,202", service["log"])
        self.assertNotIn("\x1b[", service["log"])

    def test_the_network_key_never_leaves_the_pi(self):
        self.ask(self.setup_request(25))
        self.z2m_file("configuration.yaml").write_text(
            "advanced:\n  channel: 25\n  network_key:\n" + "".join(f"    - {n}\n" for n in range(100, 116)))
        folder = install.os.open(self.data, install.os.O_RDONLY)
        self.addCleanup(install.os.close, folder)
        known = install.known_secrets(self.root, folder)
        self.assertIn(",".join(map(str, range(100, 116))), known)
        self.assertIn(bytes(range(100, 116)).hex(), known)
        for text, shown in (('"network_key": [1, 3, 5, 7, 9, 11, 13, 15, 0, 2, 4, 6, 8, 10, 12, 13]', '"network_key": [•••]'),
                            ("network_key=[1,2,3]", "network_key=•••"), ("networkKey: [9, 8]", "networkKey: [•••]")):
            self.assertEqual(install.scrub(text, set()), shown)   # also a key root does not know

    def test_an_update_of_flowthermpi_brings_the_service_up_to_date(self):
        self.ask(self.setup_request(25))
        unit = self.path(install.ZIGBEE_SERVICE_UNIT)
        current = unit.read_text()
        unit.write_text(current.replace("Restart=on-failure\n", ""))    # as an older version wrote it
        self.assertTrue(install.refresh_zigbee2mqtt_unit(self.root))
        self.assertEqual(unit.read_text(), current)
        self.assertFalse(install.refresh_zigbee2mqtt_unit(self.root))   # nothing changed: no restart

    def test_remove_takes_the_service_and_keeps_the_network(self):
        self.ask(self.setup_request(25))
        status = self.ask("remove")
        self.assertEqual(status["state"], "done")
        self.assertFalse(self.path(install.ZIGBEE_SERVICE_UNIT).exists())
        self.assertIn(("systemctl", "disable", "--now", install.ZIGBEE_SERVICE), self.pi.commands)
        self.assertTrue(self.z2m_file("configuration.yaml").exists())   # after a new installation: the same network
        self.assertEqual(status["zigbee"]["network"]["channel"], 25)


class UnpackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)

    def unpack(self, entries, comment=None):
        archive = self.tmp / "a.tar.gz"
        archive.write_bytes(tar_gz(entries, comment))
        dest = self.tmp / f"x{len(list(self.tmp.iterdir()))}"
        return install.unpack(archive, dest)

    def test_files_folders_and_inner_links(self):
        top, commit = self.unpack([("d", "p"), ("f", "p/bin/x", b"#!", 0o755), ("l", "p/y", "bin/x")], comment=COMMIT)
        self.assertEqual((top.name, commit, (top / "y").read_bytes()), ("p", COMMIT, b"#!"))
        self.assertEqual(oct((top / "bin/x").stat().st_mode & 0o777), "0o755")

    def test_nothing_lands_outside(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        for entries in ([("f", "/etc/passwd", b"x")], [("f", "p/../../x", b"x")],
                        [("d", "p"), ("l", "p/up", "/")],
                        [("d", "p"), ("l", "p/up", "../outside")],
                        [("d", "p/a/b"), ("l", "p/a/b/top", "../.."), ("l", "p/a/b/esc", "top/../..")],   # ../ after a link
                        [("d", "p"), ("l", "p/d", "."), ("f", "p/d/x", b"x")],                       # writing through a link
                        [("d", "p"), ("f", "p/x", b"1"), ("f", "p/x", b"2")]):                        # twice the same
            with self.subTest(entries=entries), self.assertRaises(install.InstallError):
                self.unpack(entries)
        self.assertEqual(list(outside.iterdir()), [])

    def test_one_top_folder(self):
        with self.assertRaises(install.InstallError):
            self.unpack([("f", "a", b"1"), ("f", "b", b"2")])


class InstallerTest(InstallBase):
    """The installer of FlowThermPi sets up what the button needs – and installs nothing of Zigbee by itself."""

    def test_fresh_install_has_the_zigbee_units(self):
        system, _ = self.run_install()
        unit = self.path(install.UNIT).read_text()
        self.assertIn("Environment=HEIZAPP_ZIGBEE_DIR=/var/lib/flowthermpi\n", unit)
        self.assertIn("PathExists=/var/lib/flowthermpi/zigbee-request.json\n", self.path(install.ZIGBEE_PATH).read_text())
        zigbee_unit = self.path(install.ZIGBEE_UNIT).read_text()
        self.assertIn("/opt/flowthermpi/current/install.py --zigbee\n", zigbee_unit)
        self.assertIn("Nice=10\n", zigbee_unit)
        self.assertIn("StartLimitBurst=30\n", zigbee_unit)   # „Neu prüfen“ many times in a row
        commands = [" ".join(c) for c in system.commands]
        self.assertIn("systemctl enable --now flowthermpi-zigbee.path", commands)
        self.assertFalse([c for c in commands if "apt" in c or "mosquitto" in c])

    def test_an_update_rewrites_the_service_of_zigbee2mqtt(self):
        self.run_install()
        unit = self.path(install.ZIGBEE_SERVICE_UNIT)
        unit.write_text(f"[Service]\nEnvironment=ZIGBEE2MQTT_CONFIG_SERIAL_PORT=/dev/serial/by-id/{STICK}\n")
        system, _ = self.run_install()
        self.assertIn("Restart=on-failure\n", unit.read_text())
        self.assertIn(f"/dev/serial/by-id/{STICK}\n", unit.read_text())      # the same stick
        self.assertIn(("systemctl", "try-restart", install.ZIGBEE_SERVICE), system.commands)
        system, _ = self.run_install()
        self.assertNotIn(("systemctl", "try-restart", install.ZIGBEE_SERVICE), system.commands)

    def test_uninstall_takes_the_zigbee_programs_along(self):
        self.run_install()
        pi = FakePi(self.root)
        pi.mosquitto = "2.0.21-1"
        for name in (install.ZIGBEE_DIR, install.ZIGBEE_DATA):
            self.path(name).mkdir(parents=True)
        self.path(install.ZIGBEE_DIR + "/installed.json").write_text('{"mosquitto_ours": true}')
        self.path(install.MOSQUITTO_CONF).parent.mkdir(parents=True)
        self.path(install.MOSQUITTO_CONF).write_text("listener 1883 127.0.0.1\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(install.main(["--root", str(self.root), "--uninstall"], pi), 0)
        self.assertFalse(self.path(install.ZIGBEE_DIR).exists() or self.path(install.MOSQUITTO_CONF).exists())
        self.assertTrue(self.path(install.ZIGBEE_DATA).exists())   # without --purge the pairing stays
        self.assertTrue([c for c in pi.commands if c[:1] == ("apt-get",) and "purge" in c])
        self.assertFalse(self.path(install.ZIGBEE_UNIT).exists() or self.path(install.ZIGBEE_PATH).exists())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(install.main(["--root", str(self.root), "--uninstall", "--purge"], FakePi(self.root)), 0)
        self.assertFalse(self.path(install.ZIGBEE_DATA).exists())


class AppBase(Base):
    """The app's side; the root service is played by a thread that runs the real install.py --zigbee."""

    def setUp(self):
        super().setUp()
        self.zigbee = self.make_zigbee()

    def make_zigbee(self):
        zigbee = Zigbee(self.data, poll=0.01)
        zigbee.pickup = 1.0
        return zigbee

    def start_service(self):
        stop = threading.Event()
        args = install.parse_args(["--root", str(self.root), "--zigbee"])

        def service():
            while not stop.is_set():
                if (self.data / install.ZIGBEE_REQUEST).exists():
                    install.zigbee_from_app(args, self.pi)
                time.sleep(0.005)

        thread = threading.Thread(target=service, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)
        self.addCleanup(self.settle)

    def settle(self):
        """Let a job that is still under way finish first (the folder is removed after the test)."""
        self.until(lambda: not (self.zigbee.view()["busy"]), seconds=10)

    def until(self, check, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(0.01)
        self.fail("timed out")

    def view_when(self, check):
        return self.until(lambda: (lambda v: v if check(v) else None)(self.zigbee.view()))


class ZigbeeAppTest(AppBase):
    def test_install_runs_in_the_background(self):
        self.pi.slow = 0.3
        self.start_service()
        view = self.zigbee.install()["zigbee"]
        self.assertEqual((view["job"]["state"], view["busy"]), ("waiting", "install"))    # answers at once
        with self.assertRaises(ActionError) as ctx:
            self.zigbee.remove()                                                         # one thing at a time
        self.assertEqual(ctx.exception.status, 409)
        view = self.view_when(lambda v: v["job"]["phase"] == "dependencies")            # the page can say what happens
        self.assertEqual((view["job"]["step"], view["job"]["steps"]), (6, 8))
        view = self.view_when(lambda v: v["job"]["state"] == "done" and not v["busy"])
        programs = view["programs"]
        self.assertEqual((programs["complete"], programs["current"], programs["mosquitto"]["active"]), (True, True, True))
        self.assertEqual(programs["installed"], {"node": "24.22.3", "pnpm": "11.26.0", "zigbee2mqtt": "2.14.3"})
        self.assertEqual(view["job"]["message"], "Die Zigbee-Programme sind installiert.")

    def test_refresh_shows_the_stick(self):
        by_id = self.root / "dev/serial/by-id"
        by_id.mkdir(parents=True)
        (by_id / STICK).symlink_to("../../ttyUSB0")
        (by_id / "usb-Prolific_USB-Serial_Controller-if00-port0").symlink_to("../../ttyUSB1")
        self.start_service()
        programs = self.zigbee.refresh()["zigbee"]["programs"]
        self.assertEqual(programs["ports"], [{"id": "usb-Prolific_USB-Serial_Controller-if00-port0", "stick": False,
                                              "device": "ttyUSB1"},
                                             {"id": STICK, "stick": True, "device": "ttyUSB0"}])
        self.assertEqual((programs["complete"], programs["current"]), (False, False))

    def test_a_failure_is_shown_with_the_pis_messages(self):
        self.pi.fail["build"] = (2, "lib/x.ts(1,1): error TS2304: Cannot find name 'y'.\n")
        self.start_service()
        self.zigbee.install()
        job = self.view_when(lambda v: v["job"]["state"] == "failed")["job"]
        self.assertEqual(job["message"], "Zigbee2MQTT ließ sich nicht bauen (Fehlercode 2).")
        self.assertIn("error TS2304", job["details"])
        self.assertFalse(self.zigbee.view()["busy"])
        again = self.make_zigbee().view()["job"]    # after a restart of the app the page still says why
        self.assertEqual((again["state"], again["message"], again["details"], again["finished"]),
                         ("failed", job["message"], job["details"], job["finished"]))
        self.zigbee.install()   # a new try is possible at once

    def test_without_the_root_service(self):
        with self.assertRaises(ActionError) as ctx:
            self.zigbee.refresh()
        self.assertEqual(ctx.exception.status, 503)
        self.assertFalse((self.data / install.ZIGBEE_REQUEST).exists())   # taken back
        self.zigbee.install()
        job = self.view_when(lambda v: v["job"]["state"] == "failed")["job"]
        self.assertIn("reagiert nicht", job["message"])

    def test_an_installation_goes_on_while_the_app_restarts(self):
        self.pi.slow = 0.3
        self.start_service()
        self.zigbee.install()
        self.view_when(lambda v: v["job"]["phase"] == "dependencies")
        again = self.make_zigbee()          # the app after its restart (e.g. an update of FlowThermPi)
        self.assertEqual((again.view()["job"]["state"], again.view()["busy"]), ("running", "install"))
        again.start()
        self.zigbee = again
        view = self.view_when(lambda v: v["job"]["state"] == "done" and not v["busy"])
        self.assertTrue(view["programs"]["complete"])

    def test_an_interrupted_installation_is_not_shown_as_running(self):
        started = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
        (self.data / install.ZIGBEE_STATUS).write_text(json.dumps(
            {"id": "0123abcd", "action": "install", "state": "running", "phase": "build", "started": started, "at": started}))
        view = self.make_zigbee().view()
        self.assertEqual((view["job"]["state"], view["busy"]), ("failed", None))
        self.assertIn("unterbrochen", view["job"]["message"])

    def test_setup_and_service_from_the_app(self):
        self.ask("install")
        self.plug()
        self.start_service()
        with self.assertRaises(ActionError) as ctx:
            self.zigbee.setup(27)
        self.assertEqual(ctx.exception.status, 400)
        result = self.zigbee.setup(20)
        self.assertEqual(result["message"], "Zigbee2MQTT ist eingerichtet (Funkkanal 20) und startet.")
        programs = result["zigbee"]["programs"]
        self.assertEqual((programs["service"]["active"], programs["network"]["channel"], programs["network"]["port"]),
                         (True, 20, STICK))
        self.assertEqual(self.zigbee.service(False)["zigbee"]["programs"]["service"]["enabled"], False)
        self.assertEqual(self.zigbee.service(True)["message"], "Zigbee2MQTT startet.")
        self.zigbee.remove()
        self.view_when(lambda v: v["job"]["state"] == "done" and not v["busy"])
        with self.assertRaises(ActionError) as ctx:
            self.zigbee.setup(25)
        self.assertEqual((ctx.exception.status, str(ctx.exception)), (502, "Bitte zuerst die Zigbee-Programme installieren."))

    def test_without_install_py_there_is_no_zigbee(self):
        zigbee = Zigbee(None)
        self.assertEqual(zigbee.view(), {"supported": False})
        with self.assertRaises(ActionError) as ctx:
            zigbee.install()
        self.assertEqual(ctx.exception.status, 409)


class ZigbeeApiTest(AppBase):
    def setUp(self):
        super().setUp()
        self.start_service()
        conf = self.tmp / "heizungen.conf"
        conf.write_text("")
        service = Service(str(conf), str(self.tmp / "data.json"), poll_interval=3600)
        self.server = make_server(service, "127.0.0.1", 0, zigbee=self.zigbee)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = None

    def call(self, method, path, body=None, expect=200):
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = f"heizung_einrichtung={self.cookie}"
        data = json.dumps(body if body is not None else {}).encode() if method != "GET" else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                status, raw, cookie = res.status, res.read(), res.headers.get("Set-Cookie")
        except urllib.error.HTTPError as err:
            with err:
                status, raw, cookie = err.code, err.read(), err.headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        payload = json.loads(raw or b"{}")
        self.assertEqual(status, expect, payload)
        return payload

    def test_zigbee_needs_the_pin(self):
        self.call("PUT", "/api/setup/pin", {"pin": "4711"})
        self.cookie = None                                                  # the residents have no PIN
        self.assertTrue(self.call("GET", "/api/zigbee", expect=403)["pin_required"])
        for path in ("/api/zigbee/refresh", "/api/zigbee/install", "/api/zigbee/remove", "/api/zigbee/setup",
                     "/api/zigbee/service", "/api/zigbee/search", "/api/zigbee/devices/0x8c65a3fffe1d0001/remove"):
            self.call("POST", path, expect=403)
        self.assertFalse((self.data / install.ZIGBEE_REQUEST).exists())    # nothing reached the Pi
        self.assertFalse((self.data / install.ZIGBEE_STATUS).exists())

    def test_api_flow(self):
        self.assertTrue(self.call("GET", "/api/state")["app"]["zigbee"])   # shows the tab
        self.assertFalse(self.call("POST", "/api/zigbee/refresh")["zigbee"]["programs"]["complete"])
        self.call("POST", "/api/zigbee/install")
        view = self.until(lambda: (lambda v: v if v["job"]["state"] == "done" and not v["busy"] else None)(
            self.call("GET", "/api/zigbee")))
        self.assertTrue(view["programs"]["current"])
        self.call("POST", "/api/zigbee/remove")
        view = self.until(lambda: (lambda v: v if v["job"]["action"] == "remove" and v["job"]["state"] == "done" else None)(
            self.call("GET", "/api/zigbee")))
        self.assertFalse(view["programs"]["complete"])
        self.assertNotIn("password", json.dumps(view))

    def test_thermostats_over_the_api(self):
        """The whole way with a simulated Mosquitto and Zigbee2MQTT: set up, search, a thermostat joins, remove it."""
        broker = FakeBroker()
        self.addCleanup(broker.close)
        z2m = FakeZ2M(broker, channel=25, join_after=0.3, interview=0.3)
        self.zigbee.net.retry = (0.05,)
        self.zigbee.net.start()
        self.addCleanup(self.zigbee.net.stop)
        self.pi.on_z2m = lambda on: z2m.start() if on else z2m.stop()
        with mock.patch.object(install, "MQTT_PORT", broker.port):
            self.call("POST", "/api/zigbee/install")
            self.until(lambda: self.call("GET", "/api/zigbee")["job"]["state"] == "done", seconds=10)
        login = json.loads((self.data / install.ZIGBEE_LOGIN).read_text())
        broker.users = {login["user"]: login["password"]}   # from now on Mosquitto lets only the app's login in
        broker.drop()
        self.until(lambda: self.call("GET", "/api/zigbee")["net"]["broker"] == "connected")
        self.call("POST", "/api/zigbee/search", {"on": True}, expect=409)              # Zigbee2MQTT is not set up yet
        self.plug()
        self.call("POST", "/api/zigbee/refresh")
        self.assertEqual(self.call("POST", "/api/zigbee/setup", {"channel": 25})["message"],
                         "Zigbee2MQTT ist eingerichtet (Funkkanal 25) und startet.")
        net = self.until(lambda: (lambda n: n if n["bridge"] == "online" else None)(self.call("GET", "/api/zigbee")["net"]))
        self.assertEqual((net["version"], net["channel"], net["devices"], net["search"]["active"]), ("2.14.3", 25, [], False))
        net = self.call("POST", "/api/zigbee/search", {"on": True})["zigbee"]["net"]
        self.assertTrue(net["search"]["active"])
        self.assertGreater(net["search"]["left"], 170)
        request = json.loads(next(p for t, p in broker.published if t == "zigbee2mqtt/bridge/request/permit_join"))
        self.assertEqual(request["time"], 180)
        net = self.until(lambda: (lambda n: n if n["devices"] and n["devices"][0]["interview"] == "done"
                                  and n["devices"][0]["battery"] else None)(self.call("GET", "/api/zigbee")["net"]))
        device = net["devices"][0]
        self.assertEqual((device["vendor"], device["model"], device["thermostat"], device["supported"], device["new"]),
                         ("SONOFF", "TRV-ZBT", True, True, True))
        self.assertEqual((device["battery"], device["signal"], device["temperature"]), (87, 132, 20.5))
        self.assertFalse(self.call("POST", "/api/zigbee/search", {"on": False})["zigbee"]["net"]["search"]["active"])
        z2m.remove_error = "Failed to remove device (Error: ZDO mgmtLeaveRsp timed out after 10000ms)"
        answer = self.call("POST", f"/api/zigbee/devices/{device['ieee']}/remove", {"force": False}, expect=502)
        self.assertIn("Das Gerät hat nicht geantwortet", answer["error"])
        answer = self.call("POST", f"/api/zigbee/devices/{device['ieee']}/remove", {"force": True})
        self.assertEqual((answer["message"], answer["zigbee"]["net"]["devices"]), ("Das Thermostat ist entfernt.", []))
        self.call("POST", "/api/zigbee/devices/0x8c65a3fffe1d0001/remove", {}, expect=404)
        self.call("POST", "/api/zigbee/search", {"on": "ja"}, expect=400)
        self.assertNotIn(login["password"], json.dumps(self.call("GET", "/api/zigbee")))


if __name__ == "__main__":
    unittest.main()
