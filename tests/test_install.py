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


class InstallTest(unittest.TestCase):
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
        commands = [" ".join(c) for c in system.commands]
        self.assertTrue(any(c.startswith("useradd --system") for c in commands))
        self.assertIn("systemctl enable flowthermpi", commands)
        self.assertIn("systemctl restart flowthermpi", commands)
        self.assertIn("FlowThermPi 1.2.3 läuft", out)

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
        self.assertFalse(self.path(install.UNIT).exists())
        self.assertTrue(self.path("/var/lib/flowthermpi/heizungen.conf").exists())
        with redirect_stdout(io.StringIO()):
            install.main(["--root", str(self.root), "--uninstall", "--purge"], install.DryRun())
        self.assertFalse(self.path("/var/lib/flowthermpi").exists())


if __name__ == "__main__":
    unittest.main()
