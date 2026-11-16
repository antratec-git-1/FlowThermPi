#!/usr/bin/env python3
"""FlowThermPi auf einem Raspberry Pi einrichten oder aktualisieren – ein Befehl per SSH:

    curl -fsSL https://raw.githubusercontent.com/antratec-git-1/FlowThermPi/main/install.py | sudo python3 -

Derselbe Befehl holt später den neuesten Stand. Heizungen, Tokens, Räume, Pläne und PIN liegen
getrennt vom Programm in /var/lib/flowthermpi und bleiben bei Updates erhalten.

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
import fnmatch
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path, PurePosixPath

REPO = "antratec-git-1/FlowThermPi"
NAME = "flowthermpi"
APP_DIR = "/opt/flowthermpi"
DATA_DIR = "/var/lib/flowthermpi"
UNIT = f"/etc/systemd/system/{NAME}.service"
MIN_PYTHON = (3, 11)
KEEP_RELEASES = 3
DEFAULT_PORT = 80
DEFAULT_TZ = "Europe/Berlin"
FACTORY_TZ = {"", "UTC", "Etc/UTC", "Europe/London"}  # Raspberry Pi OS without own setting
SKIP_TOP = {".git", ".claude", "data", "heizungen.conf"}  # never part of the program: secrets, user data
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


class InstallError(Exception):
    pass


def say(text: str = "") -> None:
    print(text, flush=True)


# ---------------------------------------------------------------------- system access
class System:
    """Everything that reaches beyond our own folders (services, users, owners)."""

    live = True

    def run(self, *cmd: str, check: bool = True) -> str:
        res = subprocess.run(cmd, text=True, capture_output=True)
        if check and res.returncode != 0:
            raise InstallError(f"„{' '.join(cmd)}“ ist fehlgeschlagen: {(res.stderr or res.stdout).strip()}")
        return res.stdout.strip()

    def ok(self, *cmd: str) -> bool:
        return subprocess.run(cmd, capture_output=True).returncode == 0

    def user_exists(self) -> bool:
        import pwd
        try:
            pwd.getpwnam(NAME)
            return True
        except KeyError:
            return False

    def chown(self, path: Path) -> None:
        import pwd
        user = pwd.getpwnam(NAME)
        os.chown(path, user.pw_uid, user.pw_gid)


class DryRun(System):
    """For tests (--root): records commands instead of running them."""

    live = False

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, *cmd: str, check: bool = True) -> str:
        self.commands.append(cmd)
        return ""

    def ok(self, *cmd: str) -> bool:
        self.commands.append(cmd)
        return False

    def user_exists(self) -> bool:
        return False

    def chown(self, path: Path) -> None:
        pass


# ---------------------------------------------------------------------- program files
def download(ref: str, tmp: Path) -> Path:
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


def extract(archive: Path, dest: Path) -> Path:
    """Unpack only plain files and folders, nothing outside dest (no links, no ../)."""
    dest.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
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
    return tops[0]


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


# ---------------------------------------------------------------------- data, service, checks
def write_private(path: Path, text: str, system: System) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    system.chown(path)


def prepare_data(data_dir: Path, system: System, import_dir: str | None) -> None:
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    for directory in (data_dir, data_dir / "data"):
        os.chmod(directory, 0o750)
        system.chown(directory)
    config, data = data_dir / "heizungen.conf", data_dir / "data" / "data.json"
    if import_dir:
        import_files(Path(import_dir).expanduser(), config, data, system)
    if not config.exists():
        write_private(config, CONFIG_HEADER, system)
        say("• Leere Heizungsliste angelegt – Heizungen fügst du in der App unter Einrichtung › Geräte hinzu")
    for path in (config, data):
        if path.exists():
            os.chmod(path, 0o600)
            system.chown(path)


def import_files(source: Path, config: Path, data: Path, system: System) -> None:
    conf_src = source / "heizungen.conf"
    data_src = next((c for c in (source / "data.json", source / "data" / "data.json") if c.is_file()), None)
    if not conf_src.is_file() and data_src is None:
        raise InstallError(f"In {source} liegt weder heizungen.conf noch data.json.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for src, target in ((conf_src, config), (data_src, data)):
        if src is None or not src.is_file():
            continue
        if target.exists():
            backup = target.with_name(f"{target.name}.bak-{stamp}")
            shutil.copyfile(target, backup)
            os.chmod(backup, 0o600)
            system.chown(backup)
        write_private(target, src.read_text(encoding="utf-8"), system)
        say(f"• Übernommen: {src}")
    if conf_src.is_file():
        say(f"  Hinweis: {conf_src} enthält die Tokens – die Kopie dort kannst du jetzt löschen.")


def configured_port(unit: Path) -> int | None:
    if unit.exists():
        found = re.search(r"^Environment=HEIZAPP_PORT=(\d+)$", unit.read_text(encoding="utf-8"), re.M)
        if found:
            return int(found.group(1))
    return None


def write_unit(unit: Path, port: int) -> None:
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(UNIT_TEMPLATE.format(name=NAME, app=APP_DIR, data=DATA_DIR, port=port,
                                         python=sys.executable or "/usr/bin/python3"), encoding="utf-8")
    os.chmod(unit, 0o644)


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
    update = (app / "current").is_symlink()
    say(f"FlowThermPi – {'Update' if update else 'Einrichtung'}")

    with tempfile.TemporaryDirectory(prefix="flowthermpi-") as tmp:
        src = Path(args.source).expanduser().resolve() if args.source else download(args.ref, Path(tmp))
        version = program_version(src)
        release = copy_release(src, app / "releases", version)
    say(f"• Programm FlowThermPi {version} nach {APP_DIR} kopiert")

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
    write_unit(unit, port)
    system.run("systemctl", "daemon-reload")
    system.run("systemctl", "enable", NAME)
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
    say(f"  Öffnen im Heimnetz:  http://{socket.gethostname()}.local{suffix}"
        + (f"   oder   http://{address}{suffix}" if address else ""))
    say("  Update später:       denselben Befehl noch einmal ausführen")
    say(f"  Meldungen ansehen:   journalctl -u {NAME} -f")


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
    system.run("systemctl", "disable", "--now", NAME, check=False)
    unit = root / UNIT.lstrip("/")
    if unit.exists():
        unit.unlink()
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
        if args.uninstall:
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
