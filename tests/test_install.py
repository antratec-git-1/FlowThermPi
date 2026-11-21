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
        commands = [" ".join(c) for c in system.commands]
        self.assertTrue(any(c.startswith("useradd --system") for c in commands))
        self.assertIn("systemctl enable flowthermpi", commands)
        self.assertIn("systemctl enable --now flowthermpi-update.path", commands)
        self.assertIn("systemctl restart flowthermpi", commands)
        self.assertIn("FlowThermPi 1.2.3 läuft", out)
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
        for unit in (install.UNIT, install.UPDATE_UNIT, install.UPDATE_PATH):
            self.assertFalse(self.path(unit).exists(), unit)
        self.assertIn(("systemctl", "disable", "--now", "flowthermpi-update.path"), system.commands)
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
        self.assertIn("FlowThermPi 1.3.0 läuft", out)        # the new version's installer did the work

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


if __name__ == "__main__":
    unittest.main()
