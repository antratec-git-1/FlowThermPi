"""Updates per Knopf (Einrichtung › Über): neueste Version auf GitHub finden und das Update anstoßen.

Die App hat keine Rechte am Programmordner. Für ein Update legt sie nur update-request.json in ihren
Datenordner. flowthermpi-update.path startet daraufhin als root das install.py der installierten
Version; das holt immer den neuesten Stand von GitHub main – genau wie der Einzeiler. Welcher Code
installiert wird, kann die App also nicht bestimmen. Fortschritt und Ergebnis schreibt der Installer
nach update-status.json.

Den Knopf gibt es nur, wenn FlowThermPi mit install.py eingerichtet ist: dessen systemd-Unit setzt
HEIZAPP_UPDATE_DIR. Auf GitHub wird nur gefragt, wenn jemand „Nach Updates suchen“ drückt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import REPO_URL, __version__
from .service import ActionError

REPO = REPO_URL.removeprefix("https://github.com/")
BRANCH = "main"
REQUEST, STATUS, RELEASE_INFO = "update-request.json", "update-status.json", "release.json"
COMMIT = re.compile(r"[0-9a-f]{40}")
APP_ROOT = Path(__file__).resolve().parent.parent   # the installed release (…/releases/0004-0.2.0)
NOT_PICKED_UP = timedelta(minutes=2)   # the request should be gone within seconds
STALE = timedelta(minutes=20)          # "running" for longer: interrupted (the update service may run 15 min)
MAX_ANSWER = 1 << 20
log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now().astimezone()


def _when(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


class Updater:
    def __init__(self, folder: str | os.PathLike | None, app_root: Path = APP_ROOT,
                 opener: Callable = urllib.request.urlopen):
        self.folder = Path(folder) if folder else None
        self.installed = self._read_installed(Path(app_root))
        self._open = opener
        self._asked: datetime | None = None   # when this process placed a request
        self._lock = threading.Lock()

    @property
    def supported(self) -> bool:
        return self.folder is not None

    @staticmethod
    def _read_installed(app_root: Path) -> dict:
        try:
            info = json.loads((app_root / RELEASE_INFO).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = None
        info = info if isinstance(info, dict) else {}
        commit = info.get("commit")
        return {"version": __version__,
                "commit": commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else None,
                "installed": info.get("installed") if isinstance(info.get("installed"), str) else None}

    # ------------------------------------------------------------------ GitHub
    def _get(self, url: str, headers: dict | None = None) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": f"FlowThermPi/{__version__}", **(headers or {})})
        with self._open(request, timeout=15) as res:
            data = res.read(MAX_ANSWER + 1)
        if len(data) > MAX_ANSWER:
            raise ValueError("Antwort zu groß")
        return data

    def check(self) -> dict:
        """The newest version on GitHub (main) compared with the installed one."""
        try:
            return self._check()
        except ActionError as exc:
            log.warning("Nach Updates suchen: %s", exc)
            raise

    def _check(self) -> dict:
        try:
            head = json.loads(self._get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                                        {"Accept": "application/vnd.github+json"}))
            sha = head.get("sha") if isinstance(head, dict) else None
            if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
                raise ValueError("keine Commit-Kennung in der Antwort")
            init = self._get(f"https://raw.githubusercontent.com/{REPO}/{sha}/heizapp/__init__.py").decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise ActionError("GitHub lässt gerade keine weitere Abfrage zu. "
                                  "Bitte in einer Stunde noch einmal versuchen.", 502) from exc
            raise ActionError(f"GitHub antwortet mit Fehler {exc.code}.", 502) from exc
        except (OSError, ValueError) as exc:   # no network, timeout, unexpected answer
            raise ActionError("GitHub ist nicht erreichbar – hat das Gerät Internet?", 502) from exc
        commit = head.get("commit") if isinstance(head.get("commit"), dict) else {}
        committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
        found = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.M)
        lines = str(commit.get("message") or "").strip().splitlines()
        latest = {"version": found.group(1)[:40] if found else "?", "commit": sha,
                  "date": committer.get("date") if isinstance(committer.get("date"), str) else None,
                  "message": lines[0][:160] if lines else ""}
        mine = self.installed
        available = sha != mine["commit"] if mine["commit"] else latest["version"] != mine["version"]
        log.info("Nach Updates gesucht: installiert %s (%s), auf GitHub %s (%s) – %s", mine["version"],
                 (mine["commit"] or "?")[:7], latest["version"], sha[:7], "Update verfügbar" if available else "aktuell")
        return {"installed": mine, "latest": latest, "available": available, "supported": self.supported}

    # ------------------------------------------------------------------ request and status
    def start(self) -> dict:
        """Ask the installer for an update; it fetches the newest version from GitHub itself."""
        if not self.supported:
            raise ActionError("Updates per Knopf gibt es nur, wenn FlowThermPi mit install.py auf dem "
                              "Raspberry Pi eingerichtet ist. Hier bitte von Hand aktualisieren.", 409)
        with self._lock:
            current = self.status()
            if current and current["state"] in ("waiting", "running"):
                raise ActionError("Das Update läuft schon.", 409)
            now = _now()
            tmp = self.folder / f".{REQUEST}.{secrets.token_hex(4)}.tmp"
            try:
                (self.folder / STATUS).unlink(missing_ok=True)   # from now on, any status is about this update
                tmp.write_text(json.dumps({"requested": _iso(now), "from": self.installed["version"],
                                           "from_commit": self.installed["commit"]}) + "\n", encoding="utf-8")
                os.replace(tmp, self.folder / REQUEST)
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                raise ActionError(f"Das Update ließ sich nicht anstoßen ({exc.strerror}).", 500) from exc
            self._asked = now
        log.info("Update angestoßen (installiert: %s) – der Dienst auf dem Pi holt die neueste Version",
                 self.installed["version"])
        return {"message": "Update gestartet.", "update": self.status()}

    def status(self) -> dict | None:
        """The update in progress or the last one, as the installer reports it (None: nothing to show)."""
        if not self.folder:
            return None
        now = _now()
        try:
            requested = datetime.fromtimestamp((self.folder / REQUEST).stat().st_mtime).astimezone()
        except OSError:
            requested = None
        if requested is not None:   # placed, not yet picked up by flowthermpi-update.path
            if now - requested > NOT_PICKED_UP:
                return {"state": "failed", "started": _iso(requested),
                        "message": "Der Update-Dienst auf dem Pi reagiert nicht. Bitte einmal den "
                                   "Installationsbefehl per SSH ausführen (siehe Anleitung)."}
            return {"state": "waiting", "started": _iso(requested)}
        try:
            info = json.loads((self.folder / STATUS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = None
        if not isinstance(info, dict):
            if self._asked is None:
                return None
            if now - self._asked > NOT_PICKED_UP:   # picked up, but the installer never reported back
                return {"state": "failed", "started": _iso(self._asked),
                        "message": "Das Update hat nicht begonnen. Meldungen: journalctl -u flowthermpi-update"}
            return {"state": "waiting", "started": _iso(self._asked)}   # picked up a moment ago
        result = {key: info.get(key) if isinstance(info.get(key), str) else None
                  for key in ("state", "started", "finished", "from", "to", "message")}
        if result["state"] not in ("running", "done", "failed"):
            return None
        started = _when(result["started"])
        if result["state"] == "running" and (started is None or now - started > STALE):
            result.update(state="failed", message="Das Update wurde unterbrochen (Strom weg oder Neustart?).")
        return result
