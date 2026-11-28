"""Tests for install.py: everything happens inside a temporary folder, no service or user is touched."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.src = self.tmp / "src"            # looks like the developer folder, secrets included
        (self.src / "heizapp" / "__pycache__").mkdir(parents=True)
        (self.src / "data").mkdir()
        (self.src / "run.py").write_text("print('hallo')\n")
        (self.src / "heizapp" / "__init__.py").write_text('__version__ = "1.2.3"\n')
        (self.src / "heizapp" / "__pycache__" / "x.pyc").write_bytes(b"x")
        (self.src / "heizungen.conf").write_text("192.168.1.10 GEHEIMERTOKEN\n")
        (self.src / "data" / "data.json").write_text("{}")
        (self.src / "doku.pdf").write_bytes(b"%PDF")

    def run_install(self, *extra):
        system = install.DryRun()
        with redirect_stdout(io.StringIO()) as out:
            code = install.main(["--root", str(self.root), "--source", str(self.src), *extra], system)
        self.assertEqual(code, 0, out.getvalue())
        return system, out.getvalue()

    def path(self, live):
        return self.root / live.lstrip("/")


class InstallTest(Base):
    def test_fresh_install(self):
        system, out = self.run_install()
        current = self.path("/opt/flowthermpi/current")
        self.assertTrue(current.is_symlink())
        self.assertTrue((current / "run.py").is_file())
        for secret in ("heizungen.conf", "data", "doku.pdf", "heizapp/__pycache__"):
            self.assertFalse((current / secret).exists(), secret)          # never copied into the program
        config = self.path("/var/lib/flowthermpi/heizungen.conf")
        self.assertEqual(oct(config.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("GEHEIMERTOKEN", config.read_text())              # fresh, empty heater list
        unit = self.path(install.UNIT).read_text()
        self.assertIn("Environment=HEIZAPP_PORT=80\n", unit)
        self.assertIn("ExecStart=", unit)
        self.assertIn("ReadWritePaths=/var/lib/flowthermpi", unit)
        self.assertIn("Environment=HEIZAPP_UPDATE_DIR=/var/lib/flowthermpi\n", unit)   # enables the button
        self.assertIn("PathExists=/var/lib/flowthermpi/update-request.json\n", self.path(install.UPDATE_PATH).read_text())
        update_unit = self.path(install.UPDATE_UNIT).read_text()
        self.assertIn("/opt/flowthermpi/current/install.py --from-app\n", update_unit)
        self.assertIn("Type=oneshot", update_unit)
        self.assertIn("Environment=HEIZAPP_WIFI_DIR=/var/lib/flowthermpi\n", unit)     # enables Einrichtung › WLAN
        self.assertIn("PathExists=/var/lib/flowthermpi/wifi-request.json\n", self.path(install.WIFI_PATH).read_text())
        self.assertIn("/opt/flowthermpi/current/install.py --wifi\n", self.path(install.WIFI_UNIT).read_text())
        self.assertIn("Environment=HEIZAPP_REMOTE_DIR=/var/lib/flowthermpi\n", unit)   # enables Einrichtung › Fernwartung
        self.assertIn("PathExists=/var/lib/flowthermpi/remote-request.json\n", self.path(install.REMOTE_PATH).read_text())
        remote_unit = self.path(install.REMOTE_UNIT).read_text()
        self.assertIn("/opt/flowthermpi/current/install.py --remote\n", remote_unit)
        self.assertIn("StartLimitBurst=30\n", remote_unit)                    # the app asks often while a login is open
        commands = [" ".join(c) for c in system.commands]
        self.assertTrue(any(c.startswith("useradd --system") for c in commands))
        self.assertIn("systemctl enable flowthermpi", commands)
        self.assertIn("systemctl enable --now flowthermpi-update.path", commands)
        self.assertIn("systemctl enable --now flowthermpi-wifi.path", commands)
        self.assertIn("systemctl enable --now flowthermpi-remote.path", commands)
        self.assertIn("systemctl restart flowthermpi", commands)
        self.assertFalse([c for c in commands if "tailscale" in c or "apt" in c])   # Tailscale only when asked for
        self.assertIn("HoLiTherm 1.2.3 läuft", out)
        info = json.loads((current / "release.json").read_text())
        self.assertEqual((info["version"], info["commit"], info["ref"]), ("1.2.3", None, None))  # local folder

    def test_commit_of_a_source_folder_is_recorded(self):
        self.run_install("--commit", "a" * 40)
        info = json.loads(self.path("/opt/flowthermpi/current/release.json").read_text())
        self.assertEqual((info["commit"], info["ref"]), ("a" * 40, "main"))
        system = install.DryRun()
        with redirect_stdout(io.StringIO()):
            code = install.main(["--root", str(self.root), "--source", str(self.src), "--commit", "nope"], system)
        self.assertEqual(code, 1)

    def test_archive_names_its_commit(self):
        for headers, expected in (({"comment": "c" * 40}, "c" * 40), ({"comment": "kein commit"}, None), ({}, None)):
            archive = self.tmp / "a.tar.gz"
            with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT, pax_headers=headers) as tar:
                tar.add(self.src / "run.py", arcname="FlowThermPi-main/run.py")
            out = self.tmp / "out"
            shutil.rmtree(out, ignore_errors=True)
            top, commit = install.extract(archive, out)
            self.assertEqual((top.name, commit), ("FlowThermPi-main", expected))

    def test_planted_links_in_the_data_folder_are_refused(self):
        self.run_install()
        victim = self.tmp / "fremde-datei"            # stands for e.g. /etc/shadow
        victim.write_text("geheim\n")
        os.chmod(victim, 0o644)
        config = self.path("/var/lib/flowthermpi/heizungen.conf")
        config.unlink()
        config.symlink_to(victim)
        system = install.DryRun()
        with redirect_stdout(io.StringIO()) as out:
            code = install.main(["--root", str(self.root), "--source", str(self.src)], system)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("Sicherheitsgründen", out.getvalue())
        self.assertEqual(oct(victim.stat().st_mode & 0o777), "0o644")      # untouched
        self.assertEqual(victim.read_text(), "geheim\n")
        config.unlink()
        data = self.path("/var/lib/flowthermpi/data")
        shutil.rmtree(data)
        outside = self.tmp / "fremder-ordner"
        outside.mkdir(mode=0o755)
        data.symlink_to(outside)
        with redirect_stdout(io.StringIO()) as out:
            code = install.main(["--root", str(self.root), "--source", str(self.src)], install.DryRun())
        self.assertEqual(code, 1, out.getvalue())
        self.assertEqual(oct(outside.stat().st_mode & 0o777), "0o755")
        self.assertFalse((self.tmp / "fremder-ordner" / "data.json").exists())

    def test_update_keeps_data_and_port_and_prunes(self):
        self.run_install("--port", "8080")
        data = self.path("/var/lib/flowthermpi/data/data.json")
        data.write_text(json.dumps({"rooms": {"r1": {"name": "Wohnzimmer"}}}))
        config = self.path("/var/lib/flowthermpi/heizungen.conf")
        config.write_text("192.168.5.81  ABC\n")
        for _ in range(4):
            self.run_install()                                              # updates, no --port
        self.assertEqual(json.loads(data.read_text())["rooms"]["r1"]["name"], "Wohnzimmer")
        self.assertEqual(config.read_text(), "192.168.5.81  ABC\n")
        self.assertIn("HEIZAPP_PORT=8080", self.path(install.UNIT).read_text())
        releases = sorted(p.name for p in self.path("/opt/flowthermpi/releases").iterdir())
        self.assertEqual(len(releases), install.KEEP_RELEASES)
        newest = max(self.path("/opt/flowthermpi/releases").iterdir(), key=install.release_key)
        self.assertEqual(self.path("/opt/flowthermpi/current").resolve(), newest.resolve())

    def test_import_from_folder(self):
        self.run_install()
        old = self.tmp / "vom-mac"
        (old / "data").mkdir(parents=True)
        (old / "heizungen.conf").write_text("192.168.5.81  TOKEN\n")
        (old / "data" / "data.json").write_text('{"rooms": {}}')
        _, out = self.run_install("--import", str(old))
        config = self.path("/var/lib/flowthermpi/heizungen.conf")
        self.assertEqual(config.read_text(), "192.168.5.81  TOKEN\n")
        self.assertEqual(oct(config.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.path("/var/lib/flowthermpi/data/data.json").read_text(), '{"rooms": {}}')
        backups = list(self.path("/var/lib/flowthermpi").glob("heizungen.conf.bak-*"))
        self.assertEqual(len(backups), 1)                                   # the empty list from before
        self.assertIn("Tokens", out)

    def test_unsafe_archive_is_rejected(self):
        archive = self.tmp / "evil.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("../evil.txt")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"evil"))
        with self.assertRaises(install.InstallError):
            install.extract(archive, self.tmp / "out")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_uninstall_keeps_data_unless_purged(self):
        self.run_install()
        system = install.DryRun()
        with redirect_stdout(io.StringIO()):
            install.main(["--root", str(self.root), "--uninstall"], system)
        self.assertFalse(self.path("/opt/flowthermpi").exists())
        for unit in (install.UNIT, install.UPDATE_UNIT, install.UPDATE_PATH, install.WIFI_UNIT, install.WIFI_PATH,
                     install.REMOTE_UNIT, install.REMOTE_PATH):
            self.assertFalse(self.path(unit).exists(), unit)
        self.assertIn(("systemctl", "disable", "--now", "flowthermpi-update.path"), system.commands)
        self.assertIn(("systemctl", "disable", "--now", "flowthermpi-wifi.path"), system.commands)
        self.assertIn(("systemctl", "disable", "--now", "flowthermpi-remote.path"), system.commands)
        self.assertTrue(self.path("/var/lib/flowthermpi/heizungen.conf").exists())
        with redirect_stdout(io.StringIO()):
            install.main(["--root", str(self.root), "--uninstall", "--purge"], install.DryRun())
        self.assertFalse(self.path("/var/lib/flowthermpi").exists())



class UpdateFromAppTest(Base):
    """install.py --from-app, as flowthermpi-update.service runs it after the button was pressed."""

    def setUp(self):
        super().setUp()
        shutil.copy(Path(install.__file__), self.src / "install.py")   # the new version brings its installer
        self.run_install("--commit", "a" * 40)
        self.data = self.path("/var/lib/flowthermpi")
        self.downloads = []
        original = install.download
        install.download = self.fake_download
        self.addCleanup(setattr, install, "download", original)
        self.github = ("b" * 40, None)                     # what "GitHub main" is: commit, or an error

    def fake_download(self, ref, tmp):
        self.downloads.append(ref)
        commit, error = self.github
        if error:
            raise install.InstallError(error)
        dest = tmp / "src" / "FlowThermPi-main"
        shutil.copytree(self.src, dest)
        (dest / "heizapp" / "__init__.py").write_text('__version__ = "1.3.0"\n')
        return dest, commit

    def press_button(self):
        (self.data / install.UPDATE_REQUEST).write_text('{"requested": "jetzt"}\n')

    def from_app(self):
        with redirect_stdout(io.StringIO()) as out:
            code = install.main(["--root", str(self.root), "--from-app"], install.DryRun())
        status = self.data / install.UPDATE_STATUS
        return code, out.getvalue(), json.loads(status.read_text()) if status.exists() else None

    def test_button_installs_newest_version(self):
        self.press_button()
        code, out, status = self.from_app()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.downloads, ["main"])          # always main – the request cannot choose
        self.assertFalse((self.data / install.UPDATE_REQUEST).exists())
        self.assertEqual((status["state"], status["from"], status["to"], status["commit"]),
                         ("done", "1.2.3", "1.3.0", "b" * 40))
        self.assertTrue(status["finished"])
        info = json.loads(self.path("/opt/flowthermpi/current/release.json").read_text())
        self.assertEqual((info["version"], info["commit"]), ("1.3.0", "b" * 40))
        self.assertIn("HoLiTherm 1.3.0 läuft", out)        # the new version's installer did the work

    def test_same_commit_is_not_installed_again(self):
        self.github = ("a" * 40, None)
        before = sorted(p.name for p in self.path("/opt/flowthermpi/releases").iterdir())
        self.press_button()
        code, _, status = self.from_app()
        self.assertEqual((code, status["state"]), (0, "done"))
        self.assertIn("schon die neueste", status["message"])
        self.assertEqual(sorted(p.name for p in self.path("/opt/flowthermpi/releases").iterdir()), before)

    def test_nothing_happens_without_request(self):
        code, out, status = self.from_app()
        self.assertEqual((code, status, self.downloads), (0, None, []))

    def test_failure_is_reported_to_the_app(self):
        self.github = (None, "Download von GitHub fehlgeschlagen (kein Netz). Hat der Pi Internet?")
        self.press_button()
        code, _, status = self.from_app()
        self.assertEqual((code, status["state"]), (1, "failed"))
        self.assertIn("Internet", status["message"])
        self.assertEqual(oct((self.data / install.UPDATE_STATUS).stat().st_mode & 0o777), "0o600")
        info = json.loads(self.path("/opt/flowthermpi/current/release.json").read_text())
        self.assertEqual(info["commit"], "a" * 40)            # the old version stays

    def test_status_file_link_is_replaced_not_followed(self):
        victim = self.tmp / "fremde-datei"
        victim.write_text("geheim\n")
        (self.data / install.UPDATE_STATUS).symlink_to(victim)
        self.github = ("a" * 40, None)
        self.press_button()
        code, _, status = self.from_app()
        self.assertEqual((code, status["state"]), (0, "done"))
        self.assertEqual(victim.read_text(), "geheim\n")
        self.assertFalse((self.data / install.UPDATE_STATUS).is_symlink())


def nm_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


class FakeNM(install.DryRun):
    """NetworkManager, iw, ip and raspi-config as on a Raspberry Pi 4 with cable and WLAN adapter."""

    run = install.System.run     # the real error handling, hiding of the password included

    def __init__(self):
        super().__init__()
        self.country = None      # WLAN country not set yet: Raspberry Pi OS keeps the WLAN switched off
        self.cable = True
        self.air = [             # in range: name, signal, security, password
            ("Zuhause", 72, "WPA2", "richtig-geheim"),
            ("Zuhause", 41, "WPA2", "richtig-geheim"),          # second access point (repeater)
            ("Nachbar:5G", 55, "WPA2 WPA3", "nachbars-pw"),
            ("Neu-WPA3", 61, "WPA3", "nur-wpa3-pw"),
            ("Café", 30, "", ""),
            ("Firma", 50, "WPA2 802.1X", None),
            ("", 45, "WPA2", "x"),                               # hidden network: no name
        ]
        self.hidden = {"Versteckt": "versteckt-pw"}           # hidden networks that answer when asked by name
        self.profiles = {}       # uuid -> {name, ssid, psk, args}
        self.active = None       # uuid of the active WLAN connection
        self.waking = 0          # the next scans fail: the adapter is still waking up
        self.echo_psk = False    # „connection add“ fails with the password in its message
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def add_profile(self, ssid, psk, name=None):
        n = len(self.profiles) + 1
        uuid = f"{n:08x}-0000-4000-8000-{n:012x}"
        self.profiles[uuid] = {"name": name or ssid, "ssid": ssid, "psk": psk, "args": {}}
        return uuid

    def password_of(self, ssid):
        if ssid in self.hidden:
            return self.hidden[ssid]
        return next((pw for name, _, _, pw in self.air if name == ssid), False)   # False: not in range

    def execute(self, cmd, env):
        self.commands.append(cmd)
        if cmd[0] == "nmcli":
            assert env and env["LC_ALL"] == "C.UTF-8", env
            return self.nmcli(list(cmd[1:]))
        if cmd[:3] == ("iw", "reg", "get"):
            return 0, f"global\ncountry {self.country or '00'}: DFS-{'ETSI' if self.country else 'UNSET'}\n" \
                      "\t(2400 - 2483 @ 40), (N/A, 20), (N/A)\n\nphy#0 (self-managed)\ncountry 99: DFS-UNSET\n", ""
        if cmd[:3] == ("raspi-config", "nonint", "do_wifi_country"):
            self.country = cmd[3]
            return 0, "", ""
        if cmd[:3] == ("rfkill", "unblock", "wifi"):
            return 0, "", ""
        if cmd == ("ip", "-j", "-4", "addr", "show"):
            links = [{"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]}]
            if self.cable:
                links.append({"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.1.20"}]})
            links.append({"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "192.168.1.21"}] if self.active else []})
            return 0, json.dumps(links), ""
        return 127, "", f"{cmd[0]}: im Test unbekannt"

    def rows(self, fields, rows):
        return 0, "".join(":".join(nm_escape(str(r[f])) for f in fields) + "\n" for r in rows), ""

    def nmcli(self, a):
        if a[:1] == ["--wait"]:
            a = a[2:]
        if a == ["radio", "wifi", "on"]:
            return 0, "", ""
        if a[:2] == ["-t", "-f"]:
            fields, rest = a[2].split(","), a[3:]
            if rest == ["device", "status"]:
                wlan = "connected" if self.active else "disconnected" if self.country else "unavailable"
                return self.rows(fields, [
                    {"DEVICE": "eth0", "TYPE": "ethernet", "STATE": "connected" if self.cable else "unavailable",
                     "CONNECTION": "Wired connection 1" if self.cable else ""},
                    {"DEVICE": "wlan0", "TYPE": "wifi", "STATE": wlan,
                     "CONNECTION": self.profiles[self.active]["name"] if self.active else ""},
                    {"DEVICE": "p2p-dev-wlan0", "TYPE": "wifi-p2p", "STATE": "disconnected", "CONNECTION": ""},
                    {"DEVICE": "lo", "TYPE": "loopback", "STATE": "connected (externally)", "CONNECTION": "lo"}])
            if rest[:5] == ["device", "wifi", "list", "ifname", "wlan0"]:
                if not self.country:
                    return 10, "", "Error: Wi-Fi radio is disabled."   # blocked until a country is set
                if self.waking:
                    self.waking -= 1
                    return 10, "", "Error: Scanning not allowed while unavailable."
                current = self.profiles[self.active]["ssid"] if self.active else None
                air = list(self.air) + ([(current, 66, "WPA2", "")] if current in self.hidden else [])
                marked, rows = False, []
                for ssid, signal, security, _ in air:
                    in_use = ssid == current and not marked
                    marked = marked or in_use
                    rows.append({"IN-USE": "*" if in_use else " ", "SSID": ssid, "SIGNAL": signal,
                                 "SECURITY": security or "--"})
                return self.rows(fields, rows)
            if rest == ["connection", "show"]:
                rows = [{"UUID": "11111111-2222-4333-8444-555555555555", "TYPE": "802-3-ethernet",
                         "NAME": "Wired connection 1"}]
                rows += [{"UUID": u, "TYPE": "802-11-wireless", "NAME": p["name"]} for u, p in self.profiles.items()]
                return self.rows(fields, rows)
            if rest == ["connection", "show", "--active"]:
                rows = [{"UUID": u, "TYPE": "802-11-wireless", "DEVICE": "wlan0"} for u in [self.active] if u]
                return self.rows(fields, rows)
            if rest[:3] == ["connection", "show", "uuid"] and fields == ["802-11-wireless.ssid"]:
                profile = self.profiles.get(rest[3])
                if not profile:
                    return 10, "", f"Error: {rest[3]} - no such connection profile."
                return 0, f"802-11-wireless.ssid:{nm_escape(profile['ssid'])}\n", ""
        if a[:2] == ["connection", "add"]:
            args = dict(zip(a[2::2], a[3::2]))
            psk = args.get("802-11-wireless-security.psk")
            if self.echo_psk:
                return 2, "", f"Error: failed to modify 802-11-wireless-security.psk: '{psk}' is not a valid PSK."
            uuid = self.add_profile(args["802-11-wireless.ssid"], psk, args["con-name"])
            self.profiles[uuid]["args"] = args
            return 0, f"Connection '{args['con-name']}' ({uuid}) successfully added.\n", ""
        if a[:3] == ["connection", "up", "uuid"]:
            profile = self.profiles.get(a[3])
            wanted = self.password_of(profile["ssid"]) if profile else False
            self.active = None                                 # the old WLAN is left first
            if wanted is False:
                return 10, "", ("Error: Connection activation failed: The Wi-Fi network could not be found\n"
                                "Hint: use 'journalctl -xe NM_CONNECTION=x + NM_DEVICE=wlan0' to get more details.")
            if (wanted or None) != (profile["psk"] or None):
                return 4, "", "Error: Connection activation failed: Secrets were required, but not provided"
            self.active = a[3]
            return 0, "Connection successfully activated (D-Bus active path: /org/freedesktop/NetworkManager/ActiveConnection/7)\n", ""
        if a[:3] == ["connection", "delete", "uuid"]:
            profile = self.profiles.pop(a[3], None)
            if self.active == a[3]:
                self.active = None
            return (0, f"Connection '{profile['name']}' ({a[3]}) successfully deleted.\n", "") if profile else \
                   (10, "", f"Error: unknown connection '{a[3]}'.")
        return 2, "", f"Error: im Test unbekannt: {a}"


class WifiFromAppTest(Base):
    """install.py --wifi, as flowthermpi-wifi.service runs it for Einrichtung › WLAN."""

    def setUp(self):
        super().setUp()
        self.run_install()
        self.data = self.path("/var/lib/flowthermpi")
        self.nm = FakeNM()

    def ask(self, request):
        text = request if isinstance(request, str) else json.dumps({"id": "0123abcd", **request})
        (self.data / install.WIFI_REQUEST).write_text(text)
        with redirect_stdout(io.StringIO()) as out:
            code = install.main(["--root", str(self.root), "--wifi"], self.nm)
        self.assertFalse((self.data / install.WIFI_REQUEST).exists())         # always gone at once (password)
        path = self.data / install.WIFI_STATUS
        if not path.exists():
            return code, out.getvalue(), None
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        return code, out.getvalue(), json.loads(path.read_text())

    def connect(self, ssid, password="", **extra):
        return self.ask({"action": "connect", "ssid": ssid, "password": password, **extra})

    def nmcli(self, *start):
        return [c[1:] for c in self.nm.commands if c[0] == "nmcli" and c[1:1 + len(start)] == start]

    def test_reasons_in_plain_words(self):
        for detail, words in (
                ("Error: Connection activation failed: Secrets were required, but not provided", "Passwort passt nicht"),
                ("Error: Connection activation failed: The Wi-Fi network could not be found", "nicht in Reichweite"),
                ("Error: Connection activation failed: IP configuration could not be reserved (no available address, "
                 "timeout, etc.).", "keine Adresse"),
                ("Error: Timeout expired (45 seconds)", "antwortet nicht"),
                ("Error: Connection activation failed: (53) The Wi-Fi P2P peer could not be found.", "nicht in Reichweite"),
                ("Error: something new", "something new")):
            self.assertIn(words, install.wifi_reason(detail, "Zuhause"), detail)

    def test_country_without_raspi_config(self):
        original = self.nm.execute

        def execute(cmd, env):
            if cmd[0] == "raspi-config":
                raise FileNotFoundError(cmd[0])
            if cmd[:3] == ("iw", "reg", "set"):
                self.nm.country = cmd[3]
            return original(cmd, env)

        self.nm.execute = execute
        code, _, status = self.ask({"action": "scan"})
        self.assertEqual((code, status["state"], self.nm.country), (0, "done", "DE"))
        self.assertIn(("iw", "reg", "set", "DE"), self.nm.commands)

    def test_terse_fields(self):
        self.assertEqual(install.nm_fields(r"*:Nachbar\:5G:55:WPA2 WPA3"), ["*", "Nachbar:5G", "55", "WPA2 WPA3"])
        self.assertEqual(install.nm_fields(r"a\\:b\\\:c:"), ["a\\", "b\\:c", ""])

    def test_scan_sets_the_country_once_and_lists_the_networks(self):
        self.nm.waking = 2                                   # adapter needs a moment after switching on
        code, out, status = self.ask({"action": "scan", "country": "AT"})
        self.assertEqual((code, status["state"], status["id"], status["action"]), (0, "done", "0123abcd", "scan"), out)
        self.assertIn(("raspi-config", "nonint", "do_wifi_country", "AT"), self.nm.commands)
        self.assertEqual(self.nm.sleeps, [2, 2])
        names = [n["ssid"] for n in status["networks"]]
        self.assertEqual(names, ["Zuhause", "Neu-WPA3", "Nachbar:5G", "Firma", "Café"])   # strongest first, one per name
        zuhause = status["networks"][0]
        self.assertEqual((zuhause["signal"], zuhause["security"], zuhause["active"], zuhause["saved"]), (72, "WPA2", False, False))
        self.assertTrue(status["networks"][3]["enterprise"])
        self.assertEqual(status["networks"][4]["security"], "")                        # open network
        self.assertEqual(status["net"]["country"], "AT")
        self.assertEqual([(i["name"], i["type"], i["state"], i["ipv4"]) for i in status["net"]["interfaces"]],
                         [("eth0", "ethernet", "connected", ["192.168.1.20"]), ("wlan0", "wifi", "disconnected", [])])
        self.nm.commands.clear()
        self.ask({"action": "scan", "country": "DE"})        # the country is only set while there is none
        self.assertFalse([c for c in self.nm.commands if c[0] == "raspi-config"])
        self.assertEqual(self.nm.country, "AT")

    def test_connect_saves_the_network_and_reports_the_new_address(self):
        old = self.nm.add_profile("Zuhause", "altes-passwort", "preconfigured")    # e.g. from the Raspberry Pi Imager
        code, out, status = self.connect("Zuhause", "richtig-geheim", country="DE")
        self.assertEqual((code, status["state"], status["ssid"]), (0, "done", "Zuhause"), out)
        self.assertEqual(status["message"], "Verbunden mit „Zuhause“.")
        [(uuid, profile)] = self.nm.profiles.items()                              # the outdated entry is gone
        self.assertNotEqual(uuid, old)
        self.assertEqual(self.nm.active, uuid)
        args = profile["args"]
        self.assertEqual((args["con-name"], args["ifname"], args["802-11-wireless-security.key-mgmt"],
                          args["connection.autoconnect"]), ("FlowThermPi WLAN Zuhause", "wlan0", "wpa-psk", "yes"))
        wlan = status["net"]["interfaces"][1]
        self.assertEqual((wlan["state"], wlan["ssid"], wlan["signal"], wlan["ipv4"]),
                         ("connected", "Zuhause", 72, ["192.168.1.21"]))
        for text in (out, (self.data / install.WIFI_STATUS).read_text()):
            self.assertNotIn("richtig-geheim", text)
        self.assertEqual(len(self.nmcli("--wait", "45", "connection", "up")), 1)

    def test_wrong_password_brings_back_the_old_network(self):
        self.nm.country = "DE"
        old = self.nm.add_profile("Zuhause", "richtig-geheim")
        self.nm.active = old
        code, out, status = self.connect("Nachbar:5G", "falsch-geraten")
        self.assertEqual((code, status["state"]), (0, "failed"), out)
        self.assertIn("Passwort passt nicht", status["message"])
        self.assertEqual(list(self.nm.profiles), [old])                           # the failed entry is removed
        self.assertEqual(self.nm.active, old)                                       # and the old WLAN is back
        self.assertEqual(self.nmcli("--wait", "30", "connection", "up"), [("--wait", "30", "connection", "up", "uuid", old)])
        self.assertEqual(status["net"]["interfaces"][1]["ssid"], "Zuhause")
        self.assertNotIn("falsch-geraten", out + json.dumps(status))

    def test_password_in_an_error_message_is_hidden(self):
        self.nm.echo_psk = True
        code, out, status = self.connect("Zuhause", "richtig-geheim")
        self.assertEqual(status["state"], "failed")
        for text in (out, (self.data / install.WIFI_STATUS).read_text()):
            self.assertNotIn("richtig-geheim", text)

    def test_kinds_of_networks(self):
        _, _, status = self.connect("Neu-WPA3", "nur-wpa3-pw")
        self.assertEqual(status["state"], "done")
        self.assertEqual(self.nm.profiles[self.nm.active]["args"]["802-11-wireless-security.key-mgmt"], "sae")
        _, _, status = self.connect("Café")                                       # open: no password
        self.assertEqual(status["state"], "done")
        self.assertNotIn("802-11-wireless-security.key-mgmt", self.nm.profiles[self.nm.active]["args"])
        _, _, status = self.connect("Versteckt", "versteckt-pw", hidden=True)
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(self.nm.profiles[self.nm.active]["args"]["802-11-wireless.hidden"], "yes")
        self.assertEqual(status["net"]["interfaces"][1]["ssid"], "Versteckt")
        for ssid, password, words in (("Firma", "egal-was-1", "802.1X"), ("Zuhause", "", "fehlt das Passwort"),
                                      ("Weit weg", "irgendwas-1", "nicht in Reichweite")):
            before = len(self.nmcli("connection", "add"))
            _, _, status = self.connect(ssid, password)
            self.assertEqual(status["state"], "failed", ssid)
            self.assertIn(words, status["message"])
            self.assertEqual(len(self.nmcli("connection", "add")), before)          # nothing was saved

    def test_forget_removes_every_entry_of_that_network(self):
        self.nm.country = "DE"
        self.nm.add_profile("Zuhause", "a-1234567", "preconfigured")
        self.nm.add_profile("Zuhause", "b-1234567", "FlowThermPi WLAN Zuhause")
        cafe = self.nm.add_profile("Café", "")
        code, _, status = self.ask({"action": "forget", "ssid": "Zuhause"})
        self.assertEqual((code, status["state"], status["message"]), (0, "done", "„Zuhause“ ist vergessen."))
        self.assertEqual(list(self.nm.profiles), [cafe])
        _, _, status = self.ask({"action": "forget", "ssid": "Gibt es nicht"})
        self.assertEqual(status["message"], "„Gibt es nicht“ war nicht gespeichert.")
        self.assertFalse([c for c in self.nm.commands if c[0] == "raspi-config"])  # forgetting switches nothing on

    def test_forget_does_not_cut_the_pi_off(self):
        self.nm.country, self.nm.cable = "DE", False
        self.nm.active = self.nm.add_profile("Zuhause", "a-1234567", "preconfigured")
        self.nm.add_profile("Café", "")
        _, _, status = self.ask({"action": "forget", "ssid": "Zuhause"})       # the only way into the network
        self.assertEqual((status["state"], status["message"]), ("failed", install.WIFI_ONLY_LINK.format(ssid="Zuhause")))
        self.assertEqual(len(self.nm.profiles), 2)
        _, _, status = self.ask({"action": "forget", "ssid": "Café"})          # another saved WLAN may go
        self.assertEqual(status["state"], "done")
        self.assertEqual(list(self.nm.profiles), [self.nm.active])
        self.nm.cable = True                                                    # with the cable in, so may this one
        _, _, status = self.ask({"action": "forget", "ssid": "Zuhause"})
        self.assertEqual((status["state"], self.nm.profiles), ("done", {}))

    def test_bad_requests_are_refused(self):
        for text in ("kaputt", '{"action": "format"}', json.dumps({"action": "connect", "ssid": "x" * 33}),
                     json.dumps({"action": "connect", "ssid": "Zuhause", "password": "kurz"}),
                     json.dumps({"action": "connect", "ssid": "Zuhause", "password": "mit Ümlaut-1"}),
                     json.dumps({"action": "connect", "ssid": "Zeile\nzwei", "password": "richtig-geheim"}),
                     json.dumps({"action": "scan", "country": "XX"}), "[]"):
            code, out, status = self.ask(text)
            self.assertEqual((code, status["state"]), (0, "failed"), text)
            self.assertFalse(self.nmcli("connection", "add"), text)
            self.assertFalse([c for c in self.nm.commands if c[0] == "raspi-config"], text)

    def test_nothing_to_do_without_request(self):
        with redirect_stdout(io.StringIO()):
            code = install.main(["--root", str(self.root), "--wifi"], self.nm)
        self.assertEqual((code, self.nm.commands), (0, []))
        self.assertFalse((self.data / install.WIFI_STATUS).exists())

    def test_request_link_is_not_followed(self):
        victim = self.tmp / "fremde-datei"
        victim.write_text(json.dumps({"action": "forget", "ssid": "Zuhause"}))
        (self.data / install.WIFI_REQUEST).symlink_to(victim)
        with redirect_stdout(io.StringIO()) as out:
            code = install.main(["--root", str(self.root), "--wifi"], self.nm)
        self.assertEqual(code, 1)
        self.assertIn("Sicherheitsgründen", out.getvalue())
        self.assertFalse((self.data / install.WIFI_REQUEST).is_symlink())          # the link is removed ...
        self.assertTrue(victim.exists())                                            # ... not what it points to
        self.assertEqual(self.nm.commands, [])

    def test_without_networkmanager(self):
        self.nm.execute = lambda cmd, env: (_ for _ in ()).throw(FileNotFoundError(cmd[0]))
        code, _, status = self.ask({"action": "scan"})
        self.assertEqual((code, status["state"]), (0, "failed"))
        self.assertIn("NetworkManager", status["message"])


KEY = b"\x99\x02\x0d\x04" + b"k" * 600   # looks like a binary OpenPGP key
QR = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAIAAAACAAQAAAADrRVxmAAAA"
TIMEOUT = 'timeout waiting for Tailscale service to enter a Running state; check health with "tailscale status"\n'


class FakeTS(install.DryRun):
    """tailscale, tailscaled and apt as on a Raspberry Pi – Tailscale's servers included."""

    run = install.System.run     # the real error handling

    def __init__(self):
        super().__init__()
        self.installed = False
        self.daemon = True        # tailscaled answers (once installed)
        self.backend = "NoState"  # before the first „tailscale up“
        self.prefs = {"hostname": "", "accept_dns": True, "auto_update": False, "want_running": False, "control": "",
                      "tags": ""}
        self.by_hand = None       # a setting someone made on the Pi by hand, e.g. "--ssh"
        self.login = None         # login link that is open
        self.account = None       # signed in as …
        self.tags = []            # … with these tags (asked for at login and allowed by the tailnet)
        self.url_delay = 0        # this many looks until the login link is there (slow servers)
        self.apt_error = None     # what „apt-get install“ says when it fails
        self.logins = 0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def sign_in(self, account="helfer@github"):
        """Someone opens the login link (on their phone) and signs in."""
        assert self.login, "no login open"
        self.account, self.login = account, None
        self.tags = [t for t in self.prefs["tags"].split(",") if t]   # tagged at the first login: the key never expires
        self.backend = "Running" if self.prefs["want_running"] else "Stopped"

    def execute(self, cmd, env, timeout=None):
        self.commands.append(cmd)
        if cmd[0] == "tailscale":
            if not self.installed:
                raise FileNotFoundError(cmd[0])
            if not self.daemon:
                return 1, "", "failed to connect to local tailscaled; it doesn't appear to be running (sudo systemctl start tailscaled ?)\n"
            return self.tailscale(list(cmd[1:]))
        if cmd[0] == "apt-get":
            assert env and env["DEBIAN_FRONTEND"] == "noninteractive", env
            if "install" in cmd:
                if self.apt_error:
                    return 100, "Reading package lists...\n", self.apt_error
                self.installed = self.daemon = True   # the package starts tailscaled
            return 0, "", ""
        if cmd == ("systemctl", "enable", "--now", "tailscaled"):
            self.daemon = self.installed
            return 0, "", ""
        if cmd == ("systemctl", "restart", "tailscaled"):   # a login nobody finished lives only in memory
            self.daemon, self.login = self.installed, None
            if self.installed and not self.account and self.prefs["want_running"]:   # it asks for a new one
                self.logins += 1
                self.login, self.backend = f"https://login.tailscale.com/a/{self.logins:012x}", "NeedsLogin"
            return 0, "", ""
        return 127, "", f"{cmd[0]}: im Test unbekannt"

    def status_json(self):
        url = None
        if self.login:
            if self.url_delay:
                self.url_delay -= 1
            else:
                url = self.login
        me = {"ID": "nA1", "HostName": self.prefs["hostname"] or "flowthermpi", "OS": "linux", "UserID": 0,
              "TailscaleIPs": None, "Online": False}
        result = {"Version": "1.90.8-t0a1b2c3d4-g5e6f7a8b9", "BackendState": self.backend, "AuthURL": url or "",
                  "TailscaleIPs": None, "Self": me, "Health": [], "CurrentTailnet": None, "User": None}
        if self.account:
            me.update(DNSName=f"{me['HostName']}.tail1234.ts.net.", UserID=4711, Online=self.backend == "Running",
                      TailscaleIPs=["100.101.102.103", "fd7a:115c:a1e0::1"])
            if self.tags:   # a tagged device belongs to its tags, no longer to the account that signed it in
                me.update(Tags=list(self.tags), UserID=1000)
            else:
                me["KeyExpiry"] = "2027-05-20T10:00:00Z"
            user = {"ID": 1000, "LoginName": "tagged-devices"} if self.tags else {"ID": 4711, "LoginName": self.account}
            result.update(TailscaleIPs=me["TailscaleIPs"], User={str(user["ID"]): user},
                          CurrentTailnet={"Name": self.account, "MagicDNSSuffix": "tail1234.ts.net", "MagicDNSEnabled": True})
        return result

    def tailscale(self, a):
        if a == ["status", "--json"]:
            return 0, json.dumps(self.status_json(), indent=2), ""
        if a == ["up"] and self.account and self.backend != "NeedsLogin":   # just on again, settings as they are
            self.prefs["want_running"] = True
            self.backend = "Running"
            return 0, "", ""
        if a[:1] == ["up"]:
            flags = dict(f[2:].split("=", 1) if "=" in f else (f[2:], "true") for f in a[1:])
            if self.prefs["control"] and self.by_hand and self.by_hand not in a:
                return 1, "", ("Error: changing settings via 'tailscale up' requires mentioning all non-default flags. "
                               "To proceed, either re-run your command with --reset or use the command below to explicitly "
                               f"mention the current value of all non-default settings:\n\n\ttailscale up {self.by_hand}\n")
            self.prefs.update(hostname=flags.get("hostname", ""), accept_dns=flags.get("accept-dns") != "false",
                              tags=flags.get("advertise-tags", ""), want_running=True,
                              control="https://controlplane.tailscale.com")
            if self.account:
                self.backend = "Running"
                return 0, json.dumps({"BackendState": "Running"}, indent="\t") + "\n", ""
            self.logins += 1
            self.login, self.backend = f"https://login.tailscale.com/a/{self.logins:012x}", "NeedsLogin"
            if self.url_delay:   # the link comes after „up“ has given up waiting
                return 1, "", TIMEOUT
            return 1, json.dumps({"AuthURL": self.login, "QR": QR, "BackendState": "NeedsLogin"}, indent="\t") + "\n", TIMEOUT
        if a == ["set", "--auto-update=true"]:
            self.prefs["auto_update"] = True
            return 0, "", ""
        if a == ["down"]:
            self.prefs["want_running"] = False
            if self.backend in ("Running", "Starting"):
                self.backend = "Stopped"
            return 0, "", ""
        if a == ["logout"]:
            if not self.account:   # as tailscaled: without a finished login there is no key to log out – an open
                return 0, "", ""   # login link stays valid
            self.account = self.login = None
            self.tags = []
            self.backend = "NeedsLogin"
            return 0, "", ""
        return 1, "", f"tailscale: im Test unbekannt: {a}"


def pi_root(root: Path, serial: bytes | None = b"10000000a1b2c3d4\x00", distro="debian", codename="trixie") -> None:
    """The files of a Raspberry Pi that the remote maintenance reads."""
    if serial is not None:
        path = root / "sys/firmware/devicetree/base/serial-number"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(serial)
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "etc/os-release").write_text(f'PRETTY_NAME="Debian GNU/Linux"\nID={distro}\nVERSION_CODENAME={codename}\n')


class RemoteFromAppTest(Base):
    """install.py --remote, as flowthermpi-remote.service runs it for Einrichtung › Fernwartung."""

    def setUp(self):
        super().setUp()
        self.run_install()
        self.data = self.path("/var/lib/flowthermpi")
        pi_root(self.root)
        self.ts = FakeTS()
        self.fetched = []
        self.key = KEY
        original = install.fetch
        install.fetch = self.fake_fetch
        self.addCleanup(setattr, install, "fetch", original)

    def fake_fetch(self, url, missing, limit=1 << 16):
        self.fetched.append(url)
        return self.key

    def ask(self, action, **extra):
        text = action if action.startswith("{") or action == "kaputt" else json.dumps({"id": "0123abcd", "action": action, **extra})
        (self.data / install.REMOTE_REQUEST).write_text(text)
        with redirect_stdout(io.StringIO()) as out:
            code = install.main(["--root", str(self.root), "--remote"], self.ts)
        self.assertFalse((self.data / install.REMOTE_REQUEST).exists())
        path = self.data / install.REMOTE_STATUS
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        return code, out.getvalue(), json.loads(path.read_text())

    def ran(self, *start):
        return [c for c in self.ts.commands if c[:len(start)] == start]

    def test_first_setup_installs_tailscale_and_opens_the_login(self):
        code, out, status = self.ask("up")
        self.assertEqual((code, status["state"], status["action"], status["id"]), (0, "done", "up", "0123abcd"), out)
        self.assertEqual(self.fetched, ["https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg"])
        keyring = self.path(install.TAILSCALE_KEYRING)
        self.assertEqual((keyring.read_bytes(), oct(keyring.stat().st_mode & 0o777)), (KEY, "0o644"))
        self.assertIn("deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] "
                      "https://pkgs.tailscale.com/stable/debian trixie main\n", self.path(install.TAILSCALE_SOURCE).read_text())
        install_cmd = self.ran("apt-get", "-o", "DPkg::Lock::Timeout=300", "-o")
        self.assertEqual(install_cmd[0][-4:], ("install", "-y", "tailscale", "tailscale-archive-keyring"))
        self.assertTrue(self.ran("apt-get", "-o", "DPkg::Lock::Timeout=300", "update"))
        self.assertEqual(self.ran("tailscale", "up"), [("tailscale", "up", "--json", "--qr", "--timeout=8s",
                                                        "--hostname=flowthermpi-a1b2c3d4", "--accept-dns=false",
                                                        "--advertise-tags=tag:flowthermpi")])
        self.assertEqual(self.ts.prefs["hostname"], "flowthermpi-a1b2c3d4")   # the device number from the serial
        self.assertFalse(self.ts.prefs["accept_dns"])                         # the Pi keeps the household's DNS
        self.assertTrue(self.ts.prefs["auto_update"] and status["auto_update"])
        ts = status["tailscale"]
        self.assertEqual((ts["state"], ts["auth_url"], status["qr"]), ("NeedsLogin", "https://login.tailscale.com/a/000000000001", QR))
        self.assertEqual(status["login_url"], ts["auth_url"])                 # the app knows this link is its own
        self.assertEqual((status["device"], status["phase"]), ("a1b2c3d4", None))
        self.assertEqual(status["message"], "Bitte den Pi jetzt bei Tailscale anmelden.")

    def test_signed_in_the_pi_reports_its_name_and_address(self):
        self.ask("up")
        self.ts.sign_in()
        code, _, status = self.ask("status")
        ts = status["tailscale"]
        self.assertEqual((code, status["state"], ts["state"], ts["auth_url"]), (0, "done", "Running", None))
        self.assertEqual((ts["dns_name"], ts["ips"], ts["tailnet"]),
                         ("flowthermpi-a1b2c3d4.tail1234.ts.net", ["100.101.102.103", "fd7a:115c:a1e0::1"], "helfer@github"))
        # the Pi asked for its tag at login: nobody has to tag it by hand, and its key does not expire
        self.assertEqual((ts["tags"], ts["user"], ts["key_expiry"]), (["tag:flowthermpi"], "tagged-devices", None))
        self.assertEqual((ts["version"], ts["magic_dns"]), ("1.90.8", True))
        self.assertNotIn("qr", status)
        self.assertEqual(len(self.ran("apt-get")), 2)                          # installed once, not again

    def test_status_never_installs_anything(self):
        code, _, status = self.ask("status")
        self.assertEqual((code, status["state"], status["tailscale"]), (0, "done", {"installed": False}))
        self.assertEqual((self.fetched, self.ran("apt-get")), ([], []))
        self.assertFalse(self.path(install.TAILSCALE_SOURCE).exists())
        for action in ("down", "logout"):                                     # nothing to do without Tailscale
            self.assertEqual(self.ask(action)[2]["state"], "done")
        self.assertEqual(self.ran("apt-get"), [])

    def test_switched_off_and_on_again_without_a_new_login(self):
        self.ask("up")
        self.ts.sign_in()
        _, _, status = self.ask("down")
        self.assertEqual((status["message"], status["tailscale"]["state"]), ("Die Fernwartung ist aus.", "Stopped"))
        self.ts.by_hand = "--ssh"                                             # e.g. Tailscale SSH, switched on by hand
        _, out, status = self.ask("up")
        self.assertEqual((status["state"], status["tailscale"]["state"]), ("done", "Running"), out)
        self.assertIn(("tailscale", "up"), self.ts.commands)                 # plain „up“: keeps every setting
        self.assertEqual((self.ts.logins, status["message"]), (1, "Die Fernwartung ist an."))

    def test_settings_made_by_hand_are_explained(self):
        self.ask("up")
        self.ts.sign_in()
        self.ask("logout")
        self.ts.by_hand = "--ssh"
        _, _, status = self.ask("up")
        self.assertEqual(status["state"], "failed")
        self.assertIn("von Hand geändert", status["message"])
        self.assertIn("sudo tailscale up --hostname=flowthermpi-a1b2c3d4 --accept-dns=false "
                      "--advertise-tags=tag:flowthermpi", status["message"])

    def test_on_only_switches_on(self):
        # „Fernhilfe erlauben“ in the residents' view: it switches on what is set up – nothing else
        _, _, status = self.ask("on")
        self.assertEqual((status["state"], self.fetched, self.ran("apt-get")), ("failed", [], []))
        self.assertIn("noch nicht eingerichtet", status["message"])
        self.ask("up")
        self.ts.sign_in()
        self.ask("down")
        _, out, status = self.ask("on")
        self.assertEqual((status["state"], status["tailscale"]["state"], status["message"]),
                         ("done", "Running", "Die Fernwartung ist an."), out)
        self.assertEqual(self.ran("tailscale", "up")[-1], ("tailscale", "up"))   # plain „up“: every setting stays
        self.assertEqual((self.ts.logins, len(self.ran("tailscale", "set"))), (1, 1))   # no new login, no settings
        _, _, status = self.ask("on")                                         # already on: nothing to do
        self.assertEqual((status["state"], status["tailscale"]["state"]), ("done", "Running"))

    def test_on_never_opens_a_login(self):
        self.ask("up")
        self.ts.sign_in()
        self.ask("logout")                                                    # e.g. removed from the tailnet
        commands = len(self.ts.commands)
        _, _, status = self.ask("on")
        self.assertEqual(status["state"], "failed")
        self.assertIn("nicht angemeldet", status["message"])
        self.assertEqual((self.ts.logins, self.ts.login, status["tailscale"]["auth_url"]), (1, None, None))
        self.assertNotIn("login_url", status)
        self.assertFalse([c for c in self.ts.commands[commands:] if c[:2] == ("tailscale", "up")])

    def test_logout_ends_an_open_login(self):
        self.ask("up")
        _, _, status = self.ask("logout")
        self.assertEqual((status["state"], status["message"]), ("done", "Der Pi ist bei Tailscale abgemeldet."))
        self.assertEqual((status["tailscale"]["state"], status["tailscale"]["auth_url"]), ("NeedsLogin", None))
        self.assertIn(("tailscale", "logout"), self.ts.commands)
        self.assertIn(("systemctl", "restart", "tailscaled"), self.ts.commands)   # „logout“ alone leaves the link open
        self.assertIsNone(self.ts.login)                                      # nobody can use the link any more
        self.assertFalse(self.ts.prefs["want_running"])                       # nothing comes up again by itself

    def test_logout_when_signed_in_needs_no_restart(self):
        self.ask("up")
        self.ts.sign_in()
        _, _, status = self.ask("logout")
        self.assertEqual((status["state"], status["tailscale"]["state"]), ("done", "NeedsLogin"))
        self.assertNotIn(("systemctl", "restart", "tailscaled"), self.ts.commands)

    def test_a_login_started_by_hand_is_replaced(self):
        # „sudo tailscale up“ by hand, before the app: its link would sign the Pi in under the plain name „flowthermpi“
        self.ts.installed = True
        self.ts.tailscale(["up"])
        _, _, status = self.ask("status")
        self.assertEqual(status["tailscale"]["auth_url"], "https://login.tailscale.com/a/000000000001")
        self.assertNotIn("login_url", status)                                  # not the app's own login
        _, _, status = self.ask("up")
        self.assertEqual((status["login_url"], status["tailscale"]["auth_url"], status["qr"]),
                         ("https://login.tailscale.com/a/000000000002", "https://login.tailscale.com/a/000000000002", QR))
        self.assertEqual(self.ts.prefs["hostname"], "flowthermpi-a1b2c3d4")

    def test_slow_servers_send_the_link_later(self):
        self.ts.url_delay = 3
        _, _, status = self.ask("up")
        self.assertEqual((status["state"], status["tailscale"]["auth_url"]), ("done", "https://login.tailscale.com/a/000000000001"))
        self.assertEqual(status["login_url"], "https://login.tailscale.com/a/000000000001")   # still the app's own
        self.assertNotIn("qr", status)                                         # „up“ printed no QR code this time
        self.assertEqual(self.ts.sleeps, [1, 1, 1])

    def test_tailscaled_not_running(self):
        self.ts.installed, self.ts.daemon = True, False
        _, _, status = self.ask("status")
        self.assertEqual((status["tailscale"]["installed"], status["tailscale"]["daemon"]), (True, False))
        self.assertIn("tailscaled", status["tailscale"]["problem"])
        _, _, status = self.ask("up")
        self.assertIn(("systemctl", "enable", "--now", "tailscaled"), self.ts.commands)
        self.assertEqual((status["state"], status["tailscale"]["state"]), ("done", "NeedsLogin"))
        self.assertEqual(self.ran("apt-get"), [])

    def test_only_on_raspberry_pi_os_and_debian(self):
        pi_root(self.root, distro="ubuntu", codename="noble")
        _, _, status = self.ask("up")
        self.assertEqual(status["state"], "failed")
        self.assertIn("nur unter Raspberry Pi OS oder Debian", status["message"])
        self.assertEqual((self.fetched, self.ran("apt-get")), ([], []))
        self.assertFalse(self.path(install.TAILSCALE_KEYRING).exists())
        pi_root(self.root, distro="raspbian", codename="bookworm")          # Raspberry Pi OS (32 bit)
        self.ask("up")
        self.assertEqual(self.fetched, ["https://pkgs.tailscale.com/stable/raspbian/bookworm.noarmor.gpg"])

    def test_a_web_page_is_not_taken_for_the_key(self):
        self.key = b"<!DOCTYPE html><html><body>Fehler</body></html>" * 5
        _, _, status = self.ask("up")
        self.assertEqual(status["state"], "failed")
        self.assertIn("Schlüssel", status["message"])
        self.assertFalse(self.path(install.TAILSCALE_KEYRING).exists())
        self.assertFalse(self.path(install.TAILSCALE_SOURCE).exists())
        self.assertEqual(self.ran("apt-get"), [])

    def test_apt_errors_are_reported(self):
        self.ts.apt_error = "E: Unable to locate package tailscale\n"
        _, _, status = self.ask("up")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["message"], "Tailscale ließ sich nicht installieren (E: Unable to locate package tailscale).")
        self.assertEqual(status["tailscale"], {"installed": False})

    def test_the_request_cannot_choose_anything(self):
        _, _, status = self.ask("up", hostname="fremder-name", flags=["--advertise-routes=192.168.0.0/16"])
        self.assertEqual(self.ts.prefs["hostname"], "flowthermpi-a1b2c3d4")   # extra fields are ignored
        self.assertFalse([c for c in self.ts.commands if any("routes" in part or "fremder" in part for part in c)])
        for text in ("kaputt", '{"action": "rm -rf /"}', '{"action": ["up"]}', '["up"]'):
            code, _, status = self.ask(text)
            self.assertEqual((code, status["state"]), (0, "failed"), text)
            self.assertIn("Fernwartung", status["message"])

    def test_nothing_to_do_without_request(self):
        with redirect_stdout(io.StringIO()):
            code = install.main(["--root", str(self.root), "--remote"], self.ts)
        self.assertEqual((code, self.ts.commands), (0, []))
        self.assertFalse((self.data / install.REMOTE_STATUS).exists())

    def test_request_link_is_not_followed(self):
        victim = self.tmp / "fremde-datei"
        victim.write_text('{"action": "up"}')
        (self.data / install.REMOTE_REQUEST).symlink_to(victim)
        with redirect_stdout(io.StringIO()):
            code = install.main(["--root", str(self.root), "--remote"], self.ts)
        self.assertEqual(code, 1)
        self.assertEqual(self.ts.commands, [])
        self.assertFalse((self.data / install.REMOTE_REQUEST).is_symlink())   # removed, the file it pointed to stays
        self.assertTrue(victim.exists())

    def test_device_number(self):
        self.assertEqual(install.device_number(self.root), "a1b2c3d4")
        other = self.tmp / "other"
        (other / "proc").mkdir(parents=True)
        (other / "proc/cpuinfo").write_text("processor\t: 0\nRevision\t: d03114\nSerial\t\t: 100000009F8E7D6C\nModel\t: Raspberry Pi 4\n")
        self.assertEqual(install.device_number(other), "9f8e7d6c")           # older systems: /proc/cpuinfo
        (other / "proc/cpuinfo").write_text("Serial\t\t: 0000000000000000\n")
        self.assertIsNone(install.device_number(other))                       # no serial, no machine id
        (other / "etc").mkdir()
        (other / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
        number = install.device_number(other)
        self.assertRegex(number, r"^[0-9a-f]{8}$")
        self.assertNotIn(number, "0123456789abcdef0123456789abcdef")          # derived, the machine id itself stays hidden
        self.assertEqual(install.remote_name(other), f"flowthermpi-{number}")


if __name__ == "__main__":
    unittest.main()
