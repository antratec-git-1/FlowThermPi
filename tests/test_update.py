"""Tests for the update button: heizapp.update.Updater and its API routes. No network, no installer."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from heizapp import __version__, update  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError, Service  # noqa: E402
from heizapp.update import Updater  # noqa: E402

OLD, NEW = "a" * 40, "b" * 40


class FakeGitHub:
    """Stands in for urllib.request.urlopen: answers for the commits API and raw files."""

    def __init__(self, sha=NEW, version="9.9.9", error=None):
        self.sha, self.version, self.error, self.requests = sha, version, error, []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        if request.full_url.startswith("https://api.github.com/"):
            body = {"sha": self.sha, "commit": {"message": "Update-Knopf\n\nLange Beschreibung",
                                                "committer": {"date": "2026-11-21T09:30:00Z"}}}
            return io.BytesIO(json.dumps(body).encode())
        return io.BytesIO(f'APP_NAME = "HoLiTherm"\n__version__ = "{self.version}"\n'.encode())


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = Path(tmp.name, "release")      # an installed release with its release.json
        self.data = Path(tmp.name, "data")        # the data folder (HEIZAPP_UPDATE_DIR)
        self.app.mkdir()
        self.data.mkdir()
        (self.app / "release.json").write_text(json.dumps(
            {"version": __version__, "commit": OLD, "ref": "main", "installed": "2026-11-20T18:00:00+01:00"}))

    def updater(self, github=None, folder=True):
        return Updater(self.data if folder else None, app_root=self.app, opener=github or FakeGitHub())


class UpdaterTest(Base):
    def test_installed_version_comes_from_release_file(self):
        self.assertEqual(self.updater().installed,
                         {"version": __version__, "commit": OLD, "installed": "2026-11-20T18:00:00+01:00"})
        (self.app / "release.json").write_text("kaputt")
        self.assertEqual(self.updater().installed["commit"], None)

    def test_check_finds_newer_version(self):
        github = FakeGitHub()
        result = self.updater(github).check()
        self.assertTrue(result["available"])
        self.assertEqual(result["latest"], {"version": "9.9.9", "commit": NEW, "date": "2026-11-21T09:30:00Z",
                                            "message": "Update-Knopf"})
        urls = [r.full_url for r in github.requests]
        self.assertEqual(urls, ["https://api.github.com/repos/antratec-git-1/FlowThermPi/commits/main",
                                f"https://raw.githubusercontent.com/antratec-git-1/FlowThermPi/{NEW}/heizapp/__init__.py"])
        self.assertTrue(github.requests[0].get_header("User-agent").startswith("HoLiTherm/"))

    def test_check_same_commit_is_up_to_date(self):
        self.assertFalse(self.updater(FakeGitHub(sha=OLD)).check()["available"])

    def test_check_reports_github_problems(self):
        limited = urllib.error.HTTPError("https://api.github.com", 403, "rate limit", {}, None)
        for error, words in ((limited, "keine weitere Abfrage"), (urllib.error.URLError("offline"), "Internet")):
            with self.assertRaises(ActionError) as ctx:
                self.updater(FakeGitHub(error=error)).check()
            self.assertEqual(ctx.exception.status, 502)
            self.assertIn(words, str(ctx.exception))
        with self.assertRaises(ActionError):
            self.updater(FakeGitHub(sha="../../etwas-anderes")).check()   # only a real commit id is used

    def test_start_places_request_and_waits(self):
        u = self.updater()
        (self.data / "update-status.json").write_text(json.dumps({"state": "failed", "message": "alt"}))
        res = u.start()
        self.assertEqual(res["update"]["state"], "waiting")
        request = json.loads((self.data / "update-request.json").read_text())
        self.assertEqual(request["from_commit"], OLD)
        self.assertFalse((self.data / "update-status.json").exists())    # the old result is gone
        with self.assertRaises(ActionError) as ctx:
            u.start()                                                     # once is enough
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(list(self.data.glob(".*.tmp")), [])

    def test_status_follows_the_installer(self):
        u = self.updater()
        u.start()
        (self.data / "update-request.json").unlink()                      # picked up by the update service ...
        self.assertEqual(u.status()["state"], "waiting")                  # ... which has not reported yet
        now = update._now()
        status = {"state": "running", "started": now.isoformat(), "from": "0.1.0"}
        (self.data / "update-status.json").write_text(json.dumps(status))
        self.assertEqual(u.status()["state"], "running")
        status["started"] = (now - timedelta(minutes=30)).isoformat()   # power cut during the update
        (self.data / "update-status.json").write_text(json.dumps(status))
        self.assertEqual(u.status()["state"], "failed")
        self.assertIn("unterbrochen", u.status()["message"])
        status.update(state="done", message="Version 0.2.0 ist installiert.", to="0.2.0")
        (self.data / "update-status.json").write_text(json.dumps(status))
        self.assertEqual((u.status()["state"], u.status()["to"]), ("done", "0.2.0"))

    def test_request_nobody_picks_up_is_reported(self):
        u = self.updater()
        u.start()
        old = time.time() - 300
        os.utime(self.data / "update-request.json", (old, old))
        self.assertEqual(u.status()["state"], "failed")
        self.assertIn("reagiert nicht", u.status()["message"])
        self.assertEqual(u.start()["update"]["state"], "waiting")         # trying again is allowed
        (self.data / "update-request.json").unlink()
        u._asked -= timedelta(minutes=5)                                  # picked up, but no word since
        self.assertIn("nicht begonnen", u.status()["message"])

    def test_without_update_folder_there_is_no_button(self):
        u = self.updater(folder=False)
        self.assertFalse(u.supported)
        self.assertIsNone(u.status())
        with self.assertRaises(ActionError) as ctx:
            u.start()
        self.assertEqual(ctx.exception.status, 409)


class UpdateApiTest(Base):
    """The routes: state shows what is installed, check and start need the setup (PIN)."""

    def setUp(self):
        super().setUp()
        conf = self.data / "heizungen.conf"
        conf.write_text("")
        self.service = Service(str(conf), str(self.data / "data" / "data.json"), poll_interval=3600)
        self.github = FakeGitHub()
        self.server = make_server(self.service, "127.0.0.1", 0, updater=self.updater(self.github))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = None

    def call(self, method, path, body=None, expect=200):
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = f"heizung_einrichtung={self.cookie}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                status, payload, cookie = res.status, json.loads(res.read() or b"{}"), res.headers.get("Set-Cookie")
        except urllib.error.HTTPError as err:
            with err:
                status, payload, cookie = err.code, json.loads(err.read() or b"{}"), err.headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        self.assertEqual(status, expect, payload)
        return payload

    def test_api_flow(self):
        state = self.call("GET", "/api/state")
        self.assertEqual((state["app"]["commit"], state["app"]["updates"], state["update"]), (OLD, True, None))
        self.assertTrue(self.call("POST", "/api/update/check")["available"])
        self.assertEqual(self.call("POST", "/api/update/start")["update"]["state"], "waiting")
        self.assertEqual(self.call("GET", "/api/state")["update"]["state"], "waiting")   # residents see it too
        self.call("POST", "/api/update/start", expect=409)

    def test_check_and_start_need_the_pin(self):
        self.call("PUT", "/api/setup/pin", {"pin": "4711"})
        self.cookie = None
        self.assertTrue(self.call("POST", "/api/update/check", expect=403)["pin_required"])
        self.call("POST", "/api/update/start", expect=403)
        self.assertFalse((self.data / "update-request.json").exists())
        self.assertEqual(self.github.requests, [])


if __name__ == "__main__":
    unittest.main()
