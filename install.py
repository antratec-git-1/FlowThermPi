#!/usr/bin/env python3
"""FlowThermPi auf einem Raspberry Pi einrichten oder aktualisieren – ein Befehl per SSH:

    curl -fsSL https://raw.githubusercontent.com/antratec-git-1/FlowThermPi/main/install.py | sudo python3 -

Updates danach in der App: Einrichtung › Über › „Nach Updates suchen“ – oder derselbe Befehl noch
einmal. WLAN einrichten: Einrichtung › WLAN (der Pi kann also mit Netzwerkkabel ausgeliefert werden).
Heizungen, Tokens, Räume, Pläne und PIN liegen getrennt vom Programm in /var/lib/flowthermpi und
bleiben bei Updates erhalten.

Dieses Skript ist der einzige Teil von FlowThermPi, der als root läuft – auch für den Update-Knopf
(--from-app) und die WLAN-Einrichtung (--wifi). Die App selbst hat keine Rechte am System.

Optionen hinter „python3 -“ anhängen, z. B. „… | sudo python3 - --port 8080“:
    --port N           Port der App (Standard 80: http://<name>.local ohne Portangabe)
    --ref NAME         Version (Git-Tag) oder Branch statt „main“
    --import ORDNER    heizungen.conf und data.json aus diesem Ordner übernehmen (z. B. vom Mac kopiert)
    --source ORDNER    aus einem lokalen Ordner statt von GitHub installieren
    --reset-pin        PIN der Einrichtung löschen (falls vergessen)
    --uninstall        Dienst und Programm entfernen; mit --purge auch alle Daten

Braucht nur Python 3.11+ und systemd – beides bringt Raspberry Pi OS mit.
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

REPO = "antratec-git-1/FlowThermPi"
NAME = "flowthermpi"
APP_DIR = "/opt/flowthermpi"
DATA_DIR = "/var/lib/flowthermpi"
UNIT = f"/etc/systemd/system/{NAME}.service"
UPDATE_UNIT = f"/etc/systemd/system/{NAME}-update.service"   # runs this script when the app asks for an update
UPDATE_PATH = f"/etc/systemd/system/{NAME}-update.path"      # watches for the app's request
UPDATE_REQUEST = "update-request.json"   # in DATA_DIR: placed by the app (button „Jetzt aktualisieren“)
UPDATE_STATUS = "update-status.json"     # in DATA_DIR: progress and result, for the app
WIFI_UNIT = f"/etc/systemd/system/{NAME}-wifi.service"       # runs this script for the WLAN setup in the app
WIFI_PATH = f"/etc/systemd/system/{NAME}-wifi.path"          # watches for the app's WLAN request
WIFI_REQUEST = "wifi-request.json"       # in DATA_DIR: placed by the app (Einrichtung › WLAN), may hold a password
WIFI_STATUS = "wifi-status.json"         # in DATA_DIR: result and network state, for the app – never a password
WIFI_ACTIONS = ("scan", "connect", "forget")
WIFI_COUNTRIES = ("DE", "AT", "CH", "LU", "LI", "NL", "BE", "FR", "IT", "DK", "PL", "CZ", "ES", "GB", "US")
WIFI_PROFILE = "FlowThermPi WLAN"        # name of the NetworkManager connections made here
RELEASE_INFO = "release.json"            # in each release: version and Git commit, read by the app
COMMIT = re.compile(r"[0-9a-f]{40}")
MIN_PYTHON = (3, 11)
KEEP_RELEASES = 3
DEFAULT_PORT = 80
DEFAULT_TZ = "Europe/Berlin"
FACTORY_TZ = {"", "UTC", "Etc/UTC", "Europe/London"}  # Raspberry Pi OS without own setting
SKIP_TOP = {".git", ".claude", "data", "heizungen.conf", RELEASE_INFO}  # never part of the program: secrets, user data
SKIP_ANY = {"__pycache__", ".DS_Store"}
SKIP_PATTERNS = ("*.pdf", "*.log", "*.tmp", "*.pyc")

CONFIG_HEADER = """\
# Heizungen – eine pro Zeile: IP-Adresse und Token.
# Am einfachsten in der App anlegen: Einrichtung › Geräte › „Im Netz suchen“ oder „+ Heizung“.
# Die App schreibt diese Datei dann selbst.
# IP-Adresse        Token
"""

UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Eigene Einstellungen (z. B. HEIZAPP_PASSWORD=…) gehören nach /etc/default/{name}.
[Unit]
Description=FlowThermPi – lokale Heizungssteuerung
Wants=network-online.target
After=network-online.target time-sync.target

[Service]
User={name}
Group={name}
WorkingDirectory={app}/current
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
Environment=HEIZAPP_HOST=0.0.0.0
Environment=HEIZAPP_PORT={port}
Environment=HEIZAPP_CONFIG={data}/heizungen.conf
Environment=HEIZAPP_DATA={data}/data/data.json
Environment=HEIZAPP_UPDATE_DIR={data}
Environment=HEIZAPP_WIFI_DIR={data}
EnvironmentFile=-/etc/default/{name}
ExecStart={python} {app}/current/run.py
Restart=always
RestartSec=5
UMask=0077
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={data}

[Install]
WantedBy=multi-user.target
"""

# The app runs without any rights on the program; for an update it only places {data}/update-request.json.
# This unit then runs the installer of the installed version as root. It always fetches the newest
# GitHub main of REPO – the app cannot choose what is installed.
UPDATE_UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Holt die neueste Version von GitHub, wenn die App darum bittet (Knopf „Jetzt aktualisieren“).
[Unit]
Description=FlowThermPi – Update von GitHub
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart={python} -I -B -X utf8 {app}/current/install.py --from-app
TimeoutStartSec=15min
"""

UPDATE_PATH_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Startet {name}-update.service, sobald die App eine Update-Anfrage ablegt.
[Unit]
Description=FlowThermPi – wartet auf den Update-Knopf der App

[Path]
PathExists={data}/{request}
Unit={name}-update.service

[Install]
WantedBy=multi-user.target
"""


WIFI_UNIT_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Richtet das WLAN ein, wenn die App darum bittet (Einrichtung › WLAN).
[Unit]
Description=FlowThermPi – WLAN einrichten
After=NetworkManager.service

[Service]
Type=oneshot
ExecStart={python} -I -B -X utf8 {app}/current/install.py --wifi
TimeoutStartSec=3min
"""

WIFI_PATH_TEMPLATE = """\
# Geschrieben von install.py – wird bei jedem Update neu geschrieben.
# Startet {name}-wifi.service, sobald die App eine WLAN-Anfrage ablegt.
[Unit]
Description=FlowThermPi – wartet auf WLAN-Anfragen der App

[Path]
PathExists={data}/{wifi_request}
Unit={name}-wifi.service

[Install]
WantedBy=multi-user.target
"""


class InstallError(Exception):
    pass


class CommandError(InstallError):
    """A command failed. detail is its own message, without the command line (that may hold a password)."""

    def __init__(self, shown: str, detail: str):
        super().__init__(f"„{shown}“ ist fehlgeschlagen: {detail}")
        self.detail = detail


def say(text: str = "") -> None:
    print(text, flush=True)


# ---------------------------------------------------------------------- system access
class System:
    """Everything that reaches beyond our own folders (services, users, owners)."""

    live = True

    def run(self, *cmd: str, check: bool = True, env: dict | None = None, redact: str = "") -> str:
        """Output of cmd. redact: a secret in the arguments that must not appear in an error message."""
        try:
            code, out, err = self.execute(cmd, env)
        except FileNotFoundError:
            if check:
                raise CommandError(cmd[0], "Programm nicht gefunden") from None
            return ""
        if check and code != 0:
            shown, detail = " ".join(cmd), (err or out).strip()
            if redact:
                shown, detail = shown.replace(redact, "•••"), detail.replace(redact, "•••")
            raise CommandError(shown, detail)
        return out.strip()

    def execute(self, cmd: tuple[str, ...], env: dict | None) -> tuple[int, str, str]:
        res = subprocess.run(cmd, text=True, capture_output=True, env=env)
        return res.returncode, res.stdout, res.stderr

    def ok(self, *cmd: str) -> bool:
        return subprocess.run(cmd, capture_output=True).returncode == 0

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def user_exists(self) -> bool:
        import pwd
        try:
            pwd.getpwnam(NAME)
            return True
        except KeyError:
            return False

    def fchown(self, fd: int) -> None:
        import pwd
        user = pwd.getpwnam(NAME)
        os.fchown(fd, user.pw_uid, user.pw_gid)


class DryRun(System):
    """For tests (--root): records commands instead of running them."""

    live = False

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, *cmd: str, check: bool = True, env: dict | None = None, redact: str = "") -> str:
        self.commands.append(cmd)
        return ""

    def ok(self, *cmd: str) -> bool:
        self.commands.append(cmd)
        return False

    def sleep(self, seconds: float) -> None:
        pass

    def user_exists(self) -> bool:
        return False

    def fchown(self, fd: int) -> None:
        pass


# ---------------------------------------------------------------------- program files
def download(ref: str, tmp: Path) -> tuple[Path, str | None]:
    """Program folder and Git commit of ref (GitHub writes the commit into the archive)."""
    url = f"https://codeload.github.com/{REPO}/tar.gz/{ref}"
    say(f"• Lade FlowThermPi ({ref}) von GitHub …")
    error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=60) as res:
                archive = tmp / "flowthermpi.tar.gz"
                archive.write_bytes(res.read())
            return extract(archive, tmp / "src")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise InstallError(f"„{ref}“ gibt es im Repository {REPO} nicht.") from exc
            error = exc
        except (urllib.error.URLError, OSError) as exc:
            error = exc
        time.sleep(5 * attempt)
    raise InstallError(f"Download von GitHub fehlgeschlagen ({error}). Hat der Pi Internet?")


def extract(archive: Path, dest: Path) -> tuple[Path, str | None]:
    """Unpack only plain files and folders, nothing outside dest (no links, no ../)."""
    dest.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        comment = str((tar.pax_headers or {}).get("comment", ""))  # git archive: the commit id
        commit = comment if COMMIT.fullmatch(comment) else None
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise InstallError(f"Unerwarteter Pfad im Archiv: {member.name}")
            target = dest.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    tops = list(dest.iterdir())
    if len(tops) != 1 or not tops[0].is_dir():
        raise InstallError("Das Archiv von GitHub hat einen unerwarteten Aufbau.")
    return tops[0], commit


def program_version(src: Path) -> str:
    init = src / "heizapp" / "__init__.py"
    if not (src / "run.py").is_file() or not init.is_file():
        raise InstallError(f"In {src} liegt kein FlowThermPi (run.py und heizapp/ fehlen).")
    found = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
    return found.group(1) if found else "unbekannt"


def copy_release(src: Path, releases: Path, version: str) -> Path:
    """New folder releases/<running number>-<version>; the number only grows (clock-independent)."""
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / f"{max(map(release_key, releases.iterdir()), default=0) + 1:04d}-{version}"

    def skip(directory: str, names: list[str]) -> set[str]:
        top = Path(directory) == src
        return {x for x in names if x in SKIP_ANY or (top and x in SKIP_TOP)
                or any(fnmatch.fnmatch(x, pattern) for pattern in SKIP_PATTERNS)}

    shutil.copytree(src, release, ignore=skip)
    for path in [release, *release.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    return release


def write_release_info(release: Path, version: str, commit: str | None, ref: str | None) -> None:
    """What is installed, for the app („Nach Updates suchen“ compares the commit with GitHub)."""
    info = {"version": version, "commit": commit, "ref": ref, "installed": now_text()}
    path = release / RELEASE_INFO
    path.write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def release_info(folder: Path) -> dict:
    try:
        info = json.loads((folder / RELEASE_INFO).read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def install_lock(app: Path):
    """One installer at a time (the update button and the command line could meet)."""
    app.mkdir(parents=True, exist_ok=True)
    fd = os.open(app / "install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("• Warte, bis das laufende Update fertig ist …")
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def release_key(path: Path) -> int:
    found = re.match(r"(\d+)-", path.name)
    return int(found.group(1)) if found else 0


def switch_current(app: Path, target: Path) -> Path | None:
    """Point app/current at target in one step; returns the previous target."""
    current = app / "current"
    previous = current.resolve() if current.is_symlink() else None
    link = app / "current.new"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(os.path.relpath(target.resolve(), app.resolve()))
    os.replace(link, current)
    return previous


def prune(app: Path) -> None:
    current = (app / "current").resolve()
    old = sorted((d for d in (app / "releases").iterdir() if d.is_dir() and d.resolve() != current),
                 key=release_key, reverse=True)
    for directory in old[KEEP_RELEASES - 1:]:
        shutil.rmtree(directory)


# ---------------------------------------------------------------------- data folder
# The data folder belongs to the service user, so nothing in it is trusted: root never follows a link
# there (a planted link could otherwise make root change or hand over files elsewhere). Files are opened
# relative to an opened folder with O_NOFOLLOW and replaced by renaming, never written through a path.
def open_folder(name: str | Path, system: System, parent: int | None = None) -> int:
    """Folder of the service (created if missing), 0750 and owned by it; returns an open descriptor."""
    try:
        os.mkdir(name, 0o750, dir_fd=parent)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        raise InstallError(f"{name} im Datenordner ist kein normaler Ordner (Verweis?) – "
                           "aus Sicherheitsgründen abgebrochen.") from exc
    os.fchmod(fd, 0o750)
    system.fchown(fd)
    return fd


def open_file(folder: int, name: str) -> int | None:
    """An existing plain file in the folder (None if missing) – never through a link."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(f"{name} im Datenordner ist keine normale Datei (Verweis?) – "
                           "aus Sicherheitsgründen abgebrochen.") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise InstallError(f"{name} im Datenordner ist keine normale Datei – aus Sicherheitsgründen abgebrochen.")
    return fd


def read_private(folder: int, name: str) -> str | None:
    fd = open_file(folder, name)
    if fd is None:
        return None
    with os.fdopen(fd, encoding="utf-8") as fh:
        return fh.read()


def write_private(folder: int, name: str, text: str, system: System) -> None:
    """Replace folder/name: 0600 and owned by the service. Written to a new file that is renamed into
    place, so a planted link is replaced, never followed."""
    tmp = f".{name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)
            system.fchown(fh.fileno())
        os.replace(tmp, name, src_dir_fd=folder, dst_dir_fd=folder)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=folder)
        except FileNotFoundError:
            pass
        raise


def fix_private(folder: int, name: str, system: System) -> bool:
    """An existing file: 0600 and owned by the service. False if it does not exist."""
    fd = open_file(folder, name)
    if fd is None:
        return False
    try:
        os.fchmod(fd, 0o600)
        system.fchown(fd)
    finally:
        os.close(fd)
    return True


def prepare_data(data_dir: Path, system: System, import_dir: str | None) -> None:
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    top = open_folder(data_dir, system)
    try:
        sub = open_folder("data", system, top)
        try:
            if import_dir:
                import_files(Path(import_dir).expanduser(), (top, "heizungen.conf"), (sub, "data.json"), system)
            if not fix_private(top, "heizungen.conf", system):
                write_private(top, "heizungen.conf", CONFIG_HEADER, system)
                say("• Leere Heizungsliste angelegt – Heizungen fügst du in der App unter Einrichtung › Geräte hinzu")
            fix_private(sub, "data.json", system)
        finally:
            os.close(sub)
    finally:
        os.close(top)


def import_files(source: Path, config: tuple[int, str], data: tuple[int, str], system: System) -> None:
    """config/data: (open data folder, file name)."""
    conf_src = source / "heizungen.conf"
    data_src = next((c for c in (source / "data.json", source / "data" / "data.json") if c.is_file()), None)
    if not conf_src.is_file() and data_src is None:
        raise InstallError(f"In {source} liegt weder heizungen.conf noch data.json.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for src, (folder, name) in ((conf_src, config), (data_src, data)):
        if src is None or not src.is_file():
            continue
        old = read_private(folder, name)
        if old is not None:
            write_private(folder, f"{name}.bak-{stamp}", old, system)
        write_private(folder, name, src.read_text(encoding="utf-8"), system)
        say(f"• Übernommen: {src}")
    if conf_src.is_file():
        say(f"  Hinweis: {conf_src} enthält die Tokens – die Kopie dort kannst du jetzt löschen.")


# ---------------------------------------------------------------------- service, checks
def configured_port(unit: Path) -> int | None:
    if unit.exists():
        found = re.search(r"^Environment=HEIZAPP_PORT=(\d+)$", unit.read_text(encoding="utf-8"), re.M)
        if found:
            return int(found.group(1))
    return None


def write_units(root: Path, port: int) -> None:
    """The app's service plus the units behind the update button and the WLAN setup."""
    values = {"name": NAME, "app": APP_DIR, "data": DATA_DIR, "port": port, "request": UPDATE_REQUEST,
              "wifi_request": WIFI_REQUEST, "python": sys.executable or "/usr/bin/python3"}
    for unit, template in ((UNIT, UNIT_TEMPLATE), (UPDATE_UNIT, UPDATE_UNIT_TEMPLATE), (UPDATE_PATH, UPDATE_PATH_TEMPLATE),
                           (WIFI_UNIT, WIFI_UNIT_TEMPLATE), (WIFI_PATH, WIFI_PATH_TEMPLATE)):
        path = root / unit.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(**values), encoding="utf-8")
        os.chmod(path, 0o644)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def set_timezone(system: System, wanted: str) -> None:
    """Heating plans and heater clocks follow this computer's time zone."""
    zone = system.run("timedatectl", "show", "-p", "Timezone", "--value", check=False)
    if zone in FACTORY_TZ and wanted not in FACTORY_TZ:
        system.run("timedatectl", "set-timezone", wanted)
        say(f"• Zeitzone von {zone or 'unbekannt'} auf {wanted} gestellt (Heizpläne und Heizungsuhren richten sich danach)")
    else:
        say(f"• Zeitzone: {zone}")


def wait_until_up(port: int, seconds: float = 45) -> str | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as res:
                return (json.load(res).get("app") or {}).get("version") or "?"
        except urllib.error.HTTPError as exc:
            if exc.code == 401:  # password set in /etc/default/flowthermpi – the app answers
                return "?"
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return None


def own_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # no packet is sent; only picks the outgoing interface
            return sock.getsockname()[0]
    except OSError:
        return None


# ---------------------------------------------------------------------- commands
def check_host() -> None:
    if sys.platform != "linux":
        raise InstallError("Dieses Skript ist für den Raspberry Pi (Linux) gedacht.")
    if os.geteuid() != 0:
        raise InstallError("Bitte mit sudo starten:  … | sudo python3 -")
    if not os.path.isdir("/run/systemd/system"):
        raise InstallError("systemd fehlt – FlowThermPi braucht Raspberry Pi OS oder ein anderes Linux mit systemd.")


def install(args: argparse.Namespace, system: System) -> None:
    root = Path(args.root)
    app, data_dir, unit = root / APP_DIR.lstrip("/"), root / DATA_DIR.lstrip("/"), root / UNIT.lstrip("/")
    if args.commit and not COMMIT.fullmatch(args.commit):
        raise InstallError("--commit: 40 Zeichen 0-9/a-f erwartet.")
    with install_lock(app):
        update = (app / "current").is_symlink()
        say(f"FlowThermPi – {'Update' if update else 'Einrichtung'}")

        with tempfile.TemporaryDirectory(prefix="flowthermpi-") as tmp:
            if args.source:
                src, commit = Path(args.source).expanduser().resolve(), args.commit
            else:
                src, commit = download(args.ref, Path(tmp))
            version = program_version(src)
            release = copy_release(src, app / "releases", version)
            write_release_info(release, version, commit, args.ref if commit or not args.source else None)
        say(f"• Programm FlowThermPi {version}{f' ({commit[:7]})' if commit else ''} nach {APP_DIR} kopiert")

        if not system.user_exists():
            system.run("useradd", "--system", "--user-group", "--home-dir", DATA_DIR, "--no-create-home",
                       "--shell", "/usr/sbin/nologin", NAME)
            say(f"• Benutzer „{NAME}“ für den Dienst angelegt")
        prepare_data(data_dir, system, args.import_dir)
        set_timezone(system, args.timezone)

        port = args.port or configured_port(unit) or DEFAULT_PORT
        running = system.ok("systemctl", "is-active", "--quiet", NAME)
        if system.live and not running and not port_free(port):
            shutil.rmtree(release)
            raise InstallError(f"Port {port} ist schon belegt (läuft dort ein anderer Webserver?). "
                               "Mit „--port 8080“ einen anderen Port wählen.")
        previous = switch_current(app, release)
        write_units(root, port)
        system.run("systemctl", "daemon-reload")
        system.run("systemctl", "enable", NAME)
        system.run("systemctl", "enable", "--now", f"{NAME}-update.path")
        system.run("systemctl", "enable", "--now", f"{NAME}-wifi.path")
        system.run("systemctl", "restart", NAME)

        if system.live:
            say("• Starte FlowThermPi …")
            if wait_until_up(port) is None:
                say(system.run("journalctl", "-u", NAME, "-n", "25", "--no-pager", check=False))
                if previous is not None:
                    switch_current(app, previous)
                    system.run("systemctl", "restart", NAME, check=False)
                    shutil.rmtree(release, ignore_errors=True)
                    raise InstallError("Die neue Version startet nicht – die vorherige läuft wieder (Meldungen oben).")
                raise InstallError("FlowThermPi startet nicht (Meldungen oben).")
        prune(app)

    suffix = "" if port == 80 else f":{port}"
    address = own_address()
    say("")
    say(f"✓ FlowThermPi {version} läuft.")
    say(f"  Öffnen im Heimnetz:  http://{socket.gethostname().split('.')[0]}.local{suffix}"
        + (f"   oder   http://{address}{suffix}" if address else ""))
    say("  Update später:       in der App unter Einrichtung › Über – oder diesen Befehl noch einmal")
    say(f"  Meldungen ansehen:   journalctl -u {NAME} -f")


def update_from_app(args: argparse.Namespace, system: System) -> None:
    """Run by flowthermpi-update.service when the app has placed its request (button „Jetzt aktualisieren“).

    Fetches GitHub main of REPO and lets the installer of that new version install it – exactly what the
    one-line command does. The request only says „please update“; what gets installed is decided here.
    Progress and result go to update-status.json for the app."""
    root = Path(args.root)
    app, data_dir = root / APP_DIR.lstrip("/"), root / DATA_DIR.lstrip("/")
    try:
        folder = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallError(f"Datenordner {DATA_DIR} fehlt oder ist kein Ordner ({exc.strerror}).") from exc
    try:
        try:
            os.unlink(UPDATE_REQUEST, dir_fd=folder)  # first: while it exists, the path unit starts us again
        except FileNotFoundError:
            say("Keine Update-Anfrage der App – nichts zu tun.")
            return
        before = release_info(app / "current")
        status = {"state": "running", "started": now_text(), "from": before.get("version"),
                  "from_commit": before.get("commit")}
        write_private(folder, UPDATE_STATUS, json.dumps(status, indent=1) + "\n", system)
        try:
            with tempfile.TemporaryDirectory(prefix="flowthermpi-") as tmp:
                src, commit = download(args.ref, Path(tmp))
                status.update(to=program_version(src), commit=commit)
                if commit and commit == before.get("commit"):
                    status.update(state="done", message="Das ist schon die neueste Version.")
                    say("✓ Schon die neueste Version – nichts zu tun.")
                else:
                    run_new_installer(src, commit, args)
                    status.update(state="done", message=f"Version {status['to']} ist installiert.")
        except InstallError as exc:
            status.update(state="failed", message=str(exc))
            raise
        finally:
            if status["state"] == "running":
                status.update(state="failed", message=f"Unerwarteter Fehler – Meldungen: journalctl -u {NAME}-update")
            status["finished"] = now_text()
            write_private(folder, UPDATE_STATUS, json.dumps(status, indent=1) + "\n", system)
    finally:
        os.close(folder)


def run_new_installer(src: Path, commit: str | None, args: argparse.Namespace) -> None:
    """The downloaded version installs itself: its installer may know more than this one."""
    if not (src / "install.py").is_file():
        raise InstallError("Die neue Version bringt kein install.py mit.")
    cmd = [sys.executable, "-I", "-B", "-X", "utf8", str(src / "install.py"), "--source", str(src), "--ref", args.ref]
    if commit:
        cmd += ["--commit", commit]
    if args.root != "/":
        cmd += ["--root", args.root]
    try:
        res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Das Update dauert ungewöhnlich lange und wurde abgebrochen.") from exc
    for text in (res.stdout, res.stderr):
        if text.strip():
            say(text.rstrip())
    if res.returncode != 0:
        reason = next((line.lstrip("✗ ").strip() for line in reversed(res.stdout.splitlines()) if line.startswith("✗")), "")
        raise InstallError(reason or f"Das Update ist fehlgeschlagen (Fehlercode {res.returncode}).")


# ---------------------------------------------------------------------- WLAN (Einrichtung › WLAN)
# The Pi is delivered for a network cable; the WLAN is set up in the app. The app may not change the
# network itself: it places {data}/wifi-request.json and flowthermpi-wifi.path runs „install.py --wifi“
# as root. Only three actions exist (scan, connect, forget); every value is checked here again, and
# NetworkManager is driven with argument lists, never through a shell. The password is handed to nmcli
# once („connection add“, a fraction of a second in its arguments) and never written to the status
# file or the log. A failed connection is removed again and the previous WLAN comes back.
def nm(system: System, *args: str, check: bool = True, redact: str = "") -> str:
    """nmcli with English, machine-readable output and UTF-8 network names."""
    return system.run("nmcli", *args, check=check, env={**os.environ, "LC_ALL": "C.UTF-8"}, redact=redact)


def nm_fields(line: str) -> list[str]:
    """One line of „nmcli -t“: fields split at „:“; a backslash keeps a following „:“ or backslash as it is."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def nm_rows(text: str, count: int) -> list[list[str]]:
    return [(nm_fields(line) + [""] * count)[:count] for line in text.splitlines() if line.strip()]


def check_ssid(value) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 32
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise InstallError("Der WLAN-Name ist ungültig (1 bis 32 Zeichen).")
    return value


def check_wifi_password(value) -> str:
    if value in (None, ""):
        return ""   # open network
    if not isinstance(value, str) or not (re.fullmatch(r"[\x20-\x7e]{8,63}", value) or re.fullmatch(r"[0-9a-fA-F]{64}", value)):
        raise InstallError("Das WLAN-Passwort muss 8 bis 63 Zeichen lang sein (ohne Umlaute).")
    return value


def wifi_request(text: str) -> dict:
    """The app's request, checked: nothing from it reaches nmcli unchecked."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise InstallError("Die WLAN-Anfrage der App ist unlesbar.") from exc
    if not isinstance(raw, dict) or raw.get("action") not in WIFI_ACTIONS:
        raise InstallError("Unbekannte WLAN-Anfrage.")
    request = {"action": raw["action"], "country": raw.get("country") or "DE"}
    if request["country"] not in WIFI_COUNTRIES:
        raise InstallError("Unbekanntes Land für das WLAN.")
    if raw["action"] in ("connect", "forget"):
        request["ssid"] = check_ssid(raw.get("ssid"))
    if raw["action"] == "connect":
        request["password"] = check_wifi_password(raw.get("password"))
        request["hidden"] = raw.get("hidden") is True
    return request


def request_id(text: str) -> str | None:
    try:
        rid = json.loads(text).get("id")
    except (ValueError, AttributeError):
        return None
    return rid if isinstance(rid, str) and re.fullmatch(r"[0-9a-f]{8,32}", rid) else None


def ipv4_addresses(system: System) -> dict[str, list[str]]:
    try:
        links = json.loads(system.run("ip", "-j", "-4", "addr", "show", check=False) or "[]")
    except ValueError:
        return {}
    result = {}
    for link in links if isinstance(links, list) else []:
        if isinstance(link, dict) and isinstance(link.get("ifname"), str):
            result[link["ifname"]] = [a["local"] for a in link.get("addr_info") or []
                                      if isinstance(a, dict) and a.get("family") == "inet" and isinstance(a.get("local"), str)]
    return result


def wifi_country(system: System) -> str | None:
    """WLAN country (regulatory domain); without one, Raspberry Pi OS keeps the WLAN switched off."""
    found = re.search(r"^country ([A-Z0-9]{2}):", system.run("iw", "reg", "get", check=False), re.M)
    return found.group(1) if found and found.group(1) not in ("00", "99") else None


def wifi_profiles(system: System) -> list[dict]:
    """Saved WLAN connections: uuid, name, ssid."""
    profiles = []
    for uuid, kind, name in nm_rows(nm(system, "-t", "-f", "UUID,TYPE,NAME", "connection", "show"), 3):
        if kind in ("802-11-wireless", "wifi") and uuid:
            fields = nm_fields(nm(system, "-t", "-f", "802-11-wireless.ssid", "connection", "show", "uuid", uuid,
                                  check=False).strip())
            profiles.append({"uuid": uuid, "name": name, "ssid": ":".join(fields[1:])})
    return profiles


def active_wifi(system: System, device: str) -> str | None:
    """uuid of the connection active on the WLAN adapter."""
    for uuid, kind, dev in nm_rows(nm(system, "-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active"), 3):
        if dev == device and kind in ("802-11-wireless", "wifi"):
            return uuid
    return None


def wifi_state(system: System) -> dict:
    """Cable and WLAN as NetworkManager sees them, with IPv4 addresses and the WLAN country."""
    try:
        rows = nm_rows(nm(system, "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"), 4)
    except InstallError as exc:
        raise InstallError("NetworkManager antwortet nicht – das WLAN lässt sich auf diesem Gerät nicht einrichten.") from exc
    addresses = ipv4_addresses(system)
    interfaces = []
    for device, kind, state, connection in rows:
        if kind not in ("ethernet", "wifi"):
            continue
        entry = {"name": device, "type": kind, "state": state, "ipv4": addresses.get(device, [])}
        if kind == "wifi" and state.startswith("connected"):
            for in_use, ssid, signal in nm_rows(nm(system, "-t", "-f", "IN-USE,SSID,SIGNAL", "device", "wifi", "list",
                                                   "ifname", device, "--rescan", "no", check=False), 3):
                if in_use == "*":
                    entry.update(ssid=ssid, signal=int(signal) if signal.isdigit() else None)
                    break
            else:
                entry["ssid"] = next((p["ssid"] for p in wifi_profiles(system) if p["name"] == connection), connection)
        interfaces.append(entry)
    return {"interfaces": interfaces, "country": wifi_country(system)}


def wifi_power_on(system: System, country: str, state: dict) -> None:
    """Set the country if none is set yet (then Raspberry Pi OS switches the WLAN on) and unblock the radio."""
    if not state["country"]:
        try:   # raspi-config also keeps the country for the next start
            system.run("raspi-config", "nonint", "do_wifi_country", country)
        except InstallError:
            system.run("iw", "reg", "set", country, check=False)
        state["country"] = country
        say(f"• WLAN-Land auf {country} gestellt")
    system.run("rfkill", "unblock", "wifi", check=False)
    nm(system, "radio", "wifi", "on", check=False)


def wifi_scan(system: System, device: str, rescan: bool = True) -> list[dict]:
    """Networks in range, the strongest access point per name; hidden networks (no name) are left out.
    rescan=False: what NetworkManager saw last – quick, but possibly incomplete."""
    text, error = None, None
    for attempt in range(6 if rescan else 1):   # right after switching on, the adapter needs a few seconds
        if attempt:
            system.sleep(2)
        try:
            text = nm(system, "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                      "ifname", device, "--rescan", "yes" if rescan else "no")
        except InstallError as exc:
            error = exc
            continue
        if text or attempt or not rescan:   # an empty first list: the adapter has only just woken up
            break
    if text is None:
        raise InstallError("Das WLAN des Pi ist noch nicht bereit – bitte gleich noch einmal suchen.") from error
    best: dict[str, dict] = {}
    for in_use, ssid, signal, security in nm_rows(text, 4):
        if not ssid:
            continue
        security = "" if security.strip() in ("", "--") else security.strip()
        entry = {"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0, "security": security,
                 "active": in_use == "*", "enterprise": "802.1X" in security}
        old = best.get(ssid)
        if old is None or entry["signal"] > old["signal"]:
            entry["active"] = entry["active"] or bool(old and old["active"])
            best[ssid] = entry
        elif entry["active"]:
            old["active"] = True
    saved = {p["ssid"] for p in wifi_profiles(system)}
    for entry in best.values():
        entry["saved"] = entry["ssid"] in saved
    return sorted(best.values(), key=lambda e: (not e["active"], -e["signal"], e["ssid"].lower()))[:40]


def wifi_reason(detail: str, ssid: str) -> str:
    """nmcli's error in words for the app."""
    text = detail.lower()
    if "secrets were required" in text or "802-11-wireless-security" in text or "psk" in text:
        return "Das Passwort passt nicht. Bitte noch einmal eingeben (auf Groß- und Kleinschreibung achten)."
    if re.search(r"no network with ssid|not (be )?found", text):
        return f"Das WLAN „{ssid}“ ist nicht in Reichweite."
    if "ip configuration" in text or "dhcp" in text:
        return f"Mit „{ssid}“ verbunden, aber keine Adresse vom Router bekommen."
    if "timeout" in text or "timed out" in text:
        return f"„{ssid}“ antwortet nicht – Passwort falsch oder der Pi zu weit weg?"
    first = (detail.strip().splitlines() or [""])[0].removeprefix("Error:").strip()[:160]
    return f"Verbinden hat nicht geklappt ({first or 'unbekannter Fehler'})."


def wifi_connect(system: System, request: dict, device: str) -> None:
    ssid, password = request["ssid"], request["password"]
    try:
        networks = {n["ssid"]: n for n in wifi_scan(system, device, rescan=False)}
    except InstallError:   # the WLAN was only just switched on
        networks = {}
    if ssid not in networks and not request["hidden"]:
        networks = {n["ssid"]: n for n in wifi_scan(system, device)}
    network = networks.get(ssid)
    if network is None and not request["hidden"]:
        raise InstallError(f"Das WLAN „{ssid}“ ist nicht in Reichweite.")
    if network and network["enterprise"]:
        raise InstallError("Firmen-WLAN mit eigener Anmeldung (802.1X) lässt sich hier nicht einrichten.")
    secured = bool(network["security"]) if network else bool(password)
    if secured and not password:
        raise InstallError(f"Für „{ssid}“ fehlt das Passwort.")
    wpa3_only = bool(network) and "WPA3" in network["security"] and not re.search(r"WPA[12]", network["security"])
    profiles, previous = wifi_profiles(system), active_wifi(system, device)
    args = ["connection", "add", "type", "wifi", "ifname", device, "con-name", f"{WIFI_PROFILE} {ssid}",
            "802-11-wireless.ssid", ssid, "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"]
    if request["hidden"]:
        args += ["802-11-wireless.hidden", "yes"]
    if secured:
        args += ["802-11-wireless-security.key-mgmt", "sae" if wpa3_only else "wpa-psk",
                 "802-11-wireless-security.psk", password]
    try:
        added = nm(system, *args, redact=password)
    except CommandError as exc:
        raise InstallError(wifi_reason(exc.detail, ssid)) from None
    found = re.search(r"\(([0-9a-fA-F-]{36})\)", added)
    if not found:
        raise InstallError("NetworkManager hat die neue Verbindung nicht bestätigt.")
    uuid = found.group(1)
    say(f"• Verbinde mit „{ssid}“ …")
    try:
        nm(system, "--wait", "45", "connection", "up", "uuid", uuid)
    except CommandError as exc:
        nm(system, "connection", "delete", "uuid", uuid, check=False)
        if previous:   # back to the WLAN that worked before
            nm(system, "--wait", "30", "connection", "up", "uuid", previous, check=False)
        raise InstallError(wifi_reason(exc.detail, ssid)) from None
    for profile in profiles:   # older entries for this network are outdated now (e.g. old password)
        if profile["ssid"] == ssid and profile["uuid"] != uuid:
            nm(system, "connection", "delete", "uuid", profile["uuid"], check=False)


def wifi_forget(system: System, ssid: str) -> int:
    removed = 0
    for profile in wifi_profiles(system):
        if profile["ssid"] == ssid:
            nm(system, "connection", "delete", "uuid", profile["uuid"], check=False)
            removed += 1
    return removed


def run_wifi(system: System, request: dict, status: dict) -> None:
    state = wifi_state(system)
    device = next((i["name"] for i in state["interfaces"] if i["type"] == "wifi"), None)
    if device is None:
        raise InstallError("Dieses Gerät hat keinen WLAN-Adapter, den NetworkManager verwaltet.")
    ssid = request.get("ssid")
    if request["action"] == "scan":
        wifi_power_on(system, request["country"], state)
        status["networks"] = wifi_scan(system, device)
    elif request["action"] == "connect":
        wifi_power_on(system, request["country"], state)
        wifi_connect(system, request, device)
        status["message"] = f"Verbunden mit „{ssid}“."
    else:
        removed = wifi_forget(system, ssid)
        status["message"] = f"„{ssid}“ ist vergessen." if removed else f"„{ssid}“ war nicht gespeichert."
    say(f"✓ {status.get('message') or 'WLAN-Netze gesucht.'}")


def wifi_from_app(args: argparse.Namespace, system: System) -> None:
    """Run by flowthermpi-wifi.service when the app has placed a WLAN request. The result goes to
    wifi-status.json, together with the state of cable and WLAN afterwards."""
    data_dir = Path(args.root) / DATA_DIR.lstrip("/")
    try:
        folder = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallError(f"Datenordner {DATA_DIR} fehlt oder ist kein Ordner ({exc.strerror}).") from exc
    try:
        try:
            text = read_private(folder, WIFI_REQUEST)
        finally:
            try:
                os.unlink(WIFI_REQUEST, dir_fd=folder)   # at once: it may hold a password, and the path unit restarts us
            except FileNotFoundError:
                pass
        if text is None:
            say("Keine WLAN-Anfrage der App – nichts zu tun.")
            return
        status = {"id": request_id(text), "action": None, "state": "running", "at": now_text()}
        try:
            request = wifi_request(text)
            status["action"] = request["action"]
            if request["action"] == "connect":   # takes up to a minute: the app shows „verbinde …“
                status["ssid"] = request["ssid"]
                write_private(folder, WIFI_STATUS, json.dumps(status, ensure_ascii=False, indent=1) + "\n", system)
            run_wifi(system, request, status)
            status["state"] = "done"
        except InstallError as exc:   # reported to the app; not an error of the service itself
            status.update(state="failed", message=str(exc))
            say(f"✗ {exc}")
        try:   # how things are now; for the app's list also the networks NetworkManager sees
            status["net"] = wifi_state(system)
            wifi = next((i for i in status["net"]["interfaces"] if i["type"] == "wifi"), None)
            if "networks" not in status and wifi and status["net"]["country"]:
                status["networks"] = wifi_scan(system, wifi["name"], rescan=False)
        except InstallError:
            pass
        status["at"] = now_text()
        write_private(folder, WIFI_STATUS, json.dumps(status, ensure_ascii=False, indent=1) + "\n", system)
    finally:
        os.close(folder)


def reset_pin(args: argparse.Namespace, system: System) -> None:
    system.run("systemctl", "stop", NAME, check=False)
    try:
        system.run("runuser", "-u", NAME, "--", sys.executable, f"{APP_DIR}/current/run.py",
                   "--reset-pin", "--data", f"{DATA_DIR}/data/data.json")
    finally:
        system.run("systemctl", "start", NAME, check=False)
    say("✓ PIN gelöscht – die Einrichtung ist wieder ohne PIN zugänglich.")


def uninstall(args: argparse.Namespace, system: System) -> None:
    root = Path(args.root)
    with install_lock(root / APP_DIR.lstrip("/")):
        system.run("systemctl", "disable", "--now", f"{NAME}-update.path", check=False)
        system.run("systemctl", "disable", "--now", f"{NAME}-wifi.path", check=False)
        system.run("systemctl", "disable", "--now", NAME, check=False)
        for unit in (UNIT, UPDATE_UNIT, UPDATE_PATH, WIFI_UNIT, WIFI_PATH):
            path = root / unit.lstrip("/")
            if path.exists():
                path.unlink()
        system.run("systemctl", "daemon-reload", check=False)
        shutil.rmtree(root / APP_DIR.lstrip("/"), ignore_errors=True)
    say(f"✓ FlowThermPi entfernt ({APP_DIR}).")
    if args.purge:
        shutil.rmtree(root / DATA_DIR.lstrip("/"), ignore_errors=True)
        system.run("userdel", NAME, check=False)
        say(f"✓ Daten gelöscht ({DATA_DIR}, auch die Tokens).")
    else:
        say(f"  Heizungen, Tokens und Einstellungen bleiben in {DATA_DIR} (alles löschen: --uninstall --purge).")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="install.py",
                                     description="FlowThermPi auf einem Raspberry Pi einrichten oder aktualisieren.")
    parser.add_argument("--port", type=int, help="Port der App (Standard 80, bei Updates der bisherige)")
    parser.add_argument("--ref", default="main", help="Git-Tag oder Branch (Standard: main)")
    parser.add_argument("--import", dest="import_dir", metavar="ORDNER",
                        help="heizungen.conf und data.json aus diesem Ordner übernehmen")
    parser.add_argument("--source", metavar="ORDNER", help="aus einem lokalen Ordner statt von GitHub installieren")
    parser.add_argument("--timezone", default=DEFAULT_TZ,
                        help=f"Zeitzone, falls noch die Werkseinstellung gilt (Standard {DEFAULT_TZ})")
    parser.add_argument("--reset-pin", action="store_true", help="PIN der Einrichtung löschen")
    parser.add_argument("--uninstall", action="store_true", help="Dienst und Programm entfernen")
    parser.add_argument("--purge", action="store_true", help="mit --uninstall: auch alle Daten löschen")
    parser.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)  # flowthermpi-update.service
    parser.add_argument("--wifi", action="store_true", help=argparse.SUPPRESS)  # flowthermpi-wifi.service
    parser.add_argument("--commit", help=argparse.SUPPRESS)  # with --source: Git commit of that folder
    parser.add_argument("--root", default="/", help=argparse.SUPPRESS)  # tests: work inside this folder only
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, system: System | None = None) -> int:
    args = parse_args(argv)
    if system is None:
        system = DryRun() if args.root != "/" else System()
    try:
        if sys.version_info < MIN_PYTHON:
            raise InstallError(f"Python {'.'.join(map(str, MIN_PYTHON))} oder neuer nötig, gefunden "
                               f"{platform.python_version()} – bitte ein aktuelles Raspberry Pi OS verwenden.")
        if system.live:
            check_host()
        if args.from_app:
            update_from_app(args, system)
        elif args.wifi:
            wifi_from_app(args, system)
        elif args.uninstall:
            uninstall(args, system)
        elif args.reset_pin:
            reset_pin(args, system)
        else:
            install(args, system)
        return 0
    except InstallError as exc:
        say(f"\n✗ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
